from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .browser_session import BrowserUnavailableError, persistent_context
from .config import Settings

logger = logging.getLogger(__name__)

# m.weibo.cn 请求只需要 .weibo.cn 域的 cookie；SUB 是登录态的核心凭证
COOKIE_DOMAIN = "weibo.cn"
REQUIRED_NAMES = {"SUB"}
LOGIN_URL = "https://weibo.com"
EXPORT_URL = "https://m.weibo.cn/"

_LOGIN_WAIT_SECONDS = 300
_LOGIN_POLL_SECONDS = 3

_last_refresh: float | None = None


def _domain_matches(cookie_domain: str) -> bool:
    domain = cookie_domain.lstrip(".")
    return domain == COOKIE_DOMAIN or domain.endswith("." + COOKIE_DOMAIN)


def _weibo_cookies(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in raw if _domain_matches(str(c.get("domain", "")))]


def _has_required(cookies: list[dict[str, Any]]) -> bool:
    return REQUIRED_NAMES.issubset({str(c.get("name", "")) for c in cookies})


def build_cookie_string(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))


def write_cookie_file(cookies: list[dict[str, Any]], target: str) -> None:
    path = Path(target)
    _ensure_private_directory(path.parent)
    payload = (build_cookie_string(cookies) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    except OSError:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = path.resolve()
    protected_roots = {Path.cwd().resolve(), Path.home().resolve(), Path(resolved.anchor)}
    mode = path.stat().st_mode
    if resolved not in protected_roots and not mode & stat.S_ISVTX:
        os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def cookie_is_stale(cookie_file: str, stale_before_seconds: int) -> bool:
    path = Path(cookie_file)
    if not path.exists():
        return True
    age = time.time() - path.stat().st_mtime
    return age >= stale_before_seconds


def profile_exists(settings: Settings) -> bool:
    path = Path(settings.browser_profile_dir)
    return path.is_dir() and any(path.iterdir())


async def _export_cookies(context: Any, timeout_ms: int) -> list[dict[str, Any]]:
    """访问 m.weibo.cn 让登录态经 SSO 落到 .weibo.cn 域，再导出。"""
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(EXPORT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    await asyncio.sleep(2.0)
    return _weibo_cookies(await context.cookies())


async def _is_logged_in(context: Any) -> bool:
    """用浏览器会话调 api/config 验证真实登录态。

    weibo 给未登录访客也发 SUB cookie，仅凭 SUB 存在会误判（踩过的坑）。
    """
    try:
        resp = await context.request.get("https://m.weibo.cn/api/config", timeout=15000)
        data = await resp.json()
        return bool((data.get("data") or {}).get("login"))
    except Exception as exc:
        logger.debug("login check failed: %s", exc)
        return False


async def refresh_weibo_cookie(settings: Settings) -> bool:
    """从持久 profile 无头导出新 cookie 写到 weibo_cookie_file。"""
    if not settings.weibo_cookie_file:
        logger.warning("cookie refresh needs weibo_cookie_file configured")
        return False
    timeout_ms = int(settings.browser_timeout * 1000)
    try:
        async with persistent_context(
            settings.browser_profile_dir,
            headless=settings.browser_headless,
            timeout_ms=timeout_ms,
        ) as context:
            if not await _is_logged_in(context):
                logger.warning(
                    "browser profile is not logged in; run "
                    "`python main.py --browser-login` once"
                )
                return False
            cookies = await _export_cookies(context, timeout_ms)
    except BrowserUnavailableError as exc:
        logger.warning("cookie refresh unavailable: %s", exc)
        return False
    except Exception as exc:
        logger.warning("cookie refresh failed: %s", exc)
        return False

    if not _has_required(cookies):
        logger.warning("cookie refresh export missing required cookies")
        return False

    write_cookie_file(cookies, settings.weibo_cookie_file)
    logger.info(
        "refreshed %d weibo cookies -> %s", len(cookies), settings.weibo_cookie_file
    )
    return True


async def ensure_fresh_cookie(settings: Settings) -> bool:
    """cookie 文件过期则刷新。返回是否发生了刷新。失败只记日志不抛。"""
    global _last_refresh
    if not settings.cookie_refresh_enabled or not settings.weibo_cookie_file:
        return False
    if not profile_exists(settings):
        return False  # 未登录过，不折腾浏览器
    if not cookie_is_stale(settings.weibo_cookie_file, settings.cookie_stale_seconds):
        return False

    now = time.monotonic()
    if _last_refresh is not None and now - _last_refresh < settings.cookie_refresh_min_interval:
        return False
    _last_refresh = now

    return await refresh_weibo_cookie(settings)


async def browser_login(settings: Settings) -> bool:
    """开有头浏览器让用户登录一次，登录态存进持久 profile 并导出 cookie。"""
    timeout_ms = int(settings.browser_timeout * 1000)
    try:
        async with persistent_context(
            settings.browser_profile_dir, headless=False, timeout_ms=timeout_ms
        ) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            logger.info(
                "在打开的窗口里登录微博（扫码即可），最多等 %d 秒", _LOGIN_WAIT_SECONDS
            )
            deadline = time.monotonic() + _LOGIN_WAIT_SECONDS
            cookies: list[dict[str, Any]] = []
            while time.monotonic() < deadline:
                # 必须验证真实登录（api/config login=true）；
                # 仅凭 SUB cookie 会把访客票据误判成登录
                if await _is_logged_in(context):
                    cookies = await _export_cookies(context, timeout_ms)
                    if _has_required(cookies):
                        break
                await asyncio.sleep(_LOGIN_POLL_SECONDS)
    except BrowserUnavailableError as exc:
        logger.error("cannot open browser for login: %s", exc)
        return False
    except Exception as exc:
        logger.error("browser login failed: %s", exc)
        return False

    if not _has_required(cookies):
        logger.warning("login not detected before timeout")
        return False
    if settings.weibo_cookie_file:
        write_cookie_file(cookies, settings.weibo_cookie_file)
        logger.info("saved %d cookies to %s", len(cookies), settings.weibo_cookie_file)
    return True
