from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 关闭磁盘缓存，防止仅用于 cookie 刷新的持久 profile 无限膨胀
_LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disk-cache-size=0",
]

# Chromium 崩溃后残留的锁文件会阻塞下次启动，启动前先清掉
_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")

_locks: dict[str, asyncio.Lock] = {}


class BrowserUnavailableError(RuntimeError):
    """Playwright 未安装或浏览器无法启动。"""


def _lock_for(profile_path: Path) -> asyncio.Lock:
    key = str(profile_path)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _clear_singleton_locks(profile_path: Path) -> None:
    for name in _SINGLETON_FILES:
        try:
            (profile_path / name).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("failed to clear stale %s in %s: %s", name, profile_path, exc)


@asynccontextmanager
async def persistent_context(
    profile_dir: str,
    *,
    headless: bool = True,
    timeout_ms: int = 60000,
) -> AsyncIterator[Any]:
    """启动持久化 Chromium profile 并 yield 浏览器 context。

    按 profile 目录串行化访问，启动前清理残留锁文件，退出时保证关闭。
    Playwright 未安装时抛 BrowserUnavailableError。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailableError(
            "Playwright is not installed; run uv sync and "
            "`uv run python -m playwright install chromium`."
        ) from exc

    path = Path(profile_dir)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)

    # 清理 Chromium 锁文件也必须处于同一把进程内锁内，否则第二个协程可能
    # 删掉第一个正在使用的 profile 锁。
    async with _lock_for(path):
        _clear_singleton_locks(path)
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(path),
                headless=headless,
                args=_LAUNCH_ARGS,
                timeout=timeout_ms,
            )
            try:
                yield context
            finally:
                await context.close()
