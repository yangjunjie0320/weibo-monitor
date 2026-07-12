from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .atomic_json import AtomicJsonError, atomic_write_json, load_json_object
from .config import Settings
from .models import Account
from .weibo import (
    AuthenticationError,
    RateLimitedError,
    SourceConfigurationError,
    UpstreamError,
    WeiboClient,
    WeiboError,
)

logger = logging.getLogger(__name__)

_UTC = dt.UTC
_TOKEN_CACHE: dict[str, str] = {}
_TOKEN_LOCKS: dict[str, asyncio.Lock] = {}
_RATE_MARKERS = (
    "too_many_requests",
    "rate limit",
    "rate_limit",
    "quota",
    "credits",
    "额度",
    "余额不足",
    "429",
)
_AUTH_MARKERS = (
    "unauthorized",
    "auth login",
    "登录已失效",
    "缺少登录令牌",
    "未登录",
    "401",
)
_CONFIG_MARKERS = (
    "subscription",
    "plan_not_allowed",
    "command not found",
    "not_found",
    "套餐",
    "开发者认证",
)


def _now() -> dt.datetime:
    return dt.datetime.now(_UTC)


def _iso(value: dt.datetime | None) -> str | None:
    return value.astimezone(_UTC).isoformat(timespec="seconds") if value else None


def _parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    return parsed.astimezone(_UTC)


def balanced_account_groups(accounts: list[Account], max_users: int) -> list[list[Account]]:
    """Split accounts into deterministic, balanced groups no larger than max_users."""

    if not accounts:
        return []
    group_count = (len(accounts) + max_users - 1) // max_users
    groups = [accounts[index::group_count] for index in range(group_count)]
    if any(len(group) > max_users for group in groups):
        raise ValueError("could not partition official CLI account batches")
    return groups


def _cli_path(settings: Settings) -> str:
    configured = settings.weibo_cli_path
    if "/" in configured:
        path = Path(configured)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise SourceConfigurationError(f"official CLI is not executable: {path}")
        return str(path)
    resolved = shutil.which(configured)
    if not resolved:
        raise SourceConfigurationError(f"official CLI is not installed: {configured}")
    return resolved


def check_cli_install(settings: Settings) -> None:
    """Offline dependency check used by --self-check."""

    path = _cli_path(settings)
    env = dict(os.environ)
    env["NODE_ENV"] = "production"
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            check=False,
            env=env,
            timeout=settings.weibo_cli_timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceConfigurationError("cannot execute official CLI") from exc
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise SourceConfigurationError("official CLI version check failed")
    if settings.weibo_cli_version not in output:
        raise SourceConfigurationError(
            f"official CLI version mismatch: expected {settings.weibo_cli_version}"
        )


class OfficialCliClient:
    """Official Weibo CLI adapter with batched cycle prefetching."""

    def __init__(
        self,
        settings: Settings,
        *,
        legacy_client: WeiboClient | None = None,
        read_only: bool = False,
    ) -> None:
        self._settings = settings
        self._legacy = legacy_client
        self._read_only = read_only
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._prepared = False
        self.last_cycle_requests = 0
        self._legacy_path = Path(settings.legacy_extend_state_file)
        self._legacy_state: dict[str, Any] = {
            "schema_version": 1,
            "status": "available",
            "updated_at": None,
            "blocked_until": None,
            "last_error": None,
        }
        if self._legacy_path.exists():
            self._legacy_state = load_json_object(self._legacy_path)
            self._validate_legacy_state()

    async def ensure_cookie(self) -> None:
        """Compatibility no-op: OAuth is managed by the official CLI/keychain."""

    def reload_static_cookie(self) -> None:
        if self._legacy is not None:
            self._legacy.reload_static_cookie()

    async def prepare_cycle(self, accounts: list[Account]) -> None:
        self._cache = {account.uid: [] for account in accounts}
        self.last_cycle_requests = 0
        groups = balanced_account_groups(
            accounts, self._settings.weibo_cli_max_users_per_batch
        )
        for group in groups:
            uids = [account.uid for account in group]
            payload = await self._timeline_batch(
                uids,
                count=self._settings.weibo_cli_count,
            )
            self.last_cycle_requests += 1
            requested = set(uids)
            for status in self._statuses(payload):
                uid = _status_uid(status)
                if uid in requested:
                    self._cache[uid].append(status)
        self._prepared = True
        logger.info(
            "official CLI cycle prefetched: accounts=%d batches=%d count=%d",
            len(accounts),
            len(groups),
            self._settings.weibo_cli_count,
        )

    async def timeline_page(self, uid: str, page: int) -> dict[str, Any]:
        if page != 1:
            return {"ok": 1, "statuses": []}
        if not self._prepared or uid not in self._cache:
            payload = await self._timeline_batch(
                [uid], count=self._settings.weibo_cli_count
            )
            statuses = [item for item in self._statuses(payload) if _status_uid(item) == uid]
            return {"ok": 1, "statuses": statuses}
        return {"ok": 1, "statuses": list(self._cache[uid])}

    async def probe(self, uid: str) -> dict[str, Any]:
        payload = await self._timeline_batch([uid], count=1)
        return {
            "ok": 1,
            "statuses": [item for item in self._statuses(payload) if _status_uid(item) == uid],
        }

    async def source_check(self) -> None:
        doctor = await self._invoke_json(["doctor", "--output", "json"])
        if doctor.get("ready") is not True:
            raise SourceConfigurationError("official CLI account is not ready")

        catalog = await self._invoke_json(
            [
                "commands",
                "list",
                "--all",
                "--group",
                "statuses",
                "--output",
                "json",
            ]
        )
        command = next(
            (
                item
                for item in catalog.get("commands", [])
                if isinstance(item, dict)
                and item.get("action") == "user_timeline_batch"
            ),
            None,
        )
        if not command or command.get("access") != "allowed":
            raise SourceConfigurationError(
                "official CLI command statuses user_timeline_batch is not allowed"
            )

    async def fetch_extend(self, mid: str) -> dict[str, Any]:
        if not self._settings.legacy_extend_enabled or self._legacy is None:
            return {}
        blocked_until = _parse_time(self._legacy_state.get("blocked_until"))
        if blocked_until and blocked_until > _now():
            return {}
        try:
            extend = await self._legacy.fetch_extend_strict(mid)
        except RateLimitedError as exc:
            self._block_legacy_source(
                "rate_limited", f"HTTP {exc.status_code or '-'}"
            )
            return {}
        except WeiboError as exc:
            self._block_legacy_source("upstream", type(exc).__name__)
            return {}
        if extend:
            self._legacy_state.update(
                status="available",
                updated_at=_iso(_now()),
                blocked_until=None,
                last_error=None,
            )
            self._save_legacy_state()
        return extend

    def _block_legacy_source(self, kind: str, message: str) -> None:
        blocked_until = _now() + dt.timedelta(
            seconds=self._settings.legacy_extend_cooldown_seconds
        )
        self._legacy_state.update(
            status="rate_limited",
            updated_at=_iso(_now()),
            blocked_until=_iso(blocked_until),
            last_error={"kind": kind, "message": message},
        )
        self._save_legacy_state()
        logger.warning(
            "legacy long-text source unavailable; disabled until %s kind=%s",
            _iso(blocked_until),
            kind,
        )

    async def _timeline_batch(self, uids: list[str], *, count: int) -> dict[str, Any]:
        return await self._invoke_json(
            [
                "statuses",
                "user_timeline_batch",
                "--uids",
                ",".join(uids),
                "--count",
                str(count),
                "--page",
                "1",
                "--output",
                "json",
            ]
        )

    @staticmethod
    def _statuses(payload: dict[str, Any]) -> list[dict[str, Any]]:
        statuses = payload.get("statuses")
        if not isinstance(statuses, list):
            raise UpstreamError("official CLI response has no statuses list")
        if not all(isinstance(item, dict) for item in statuses):
            raise UpstreamError("official CLI statuses contain invalid entries")
        return statuses

    async def _invoke_json(self, arguments: list[str]) -> dict[str, Any]:
        path = _cli_path(self._settings)
        token = await _get_access_token(self._settings, path)
        try:
            return await self._invoke_json_with_token(path, arguments, token)
        except AuthenticationError:
            _TOKEN_CACHE.pop(path, None)
            token = await _get_access_token(self._settings, path)
            return await self._invoke_json_with_token(path, arguments, token)

    async def _invoke_json_with_token(
        self, path: str, arguments: list[str], token: str
    ) -> dict[str, Any]:
        env = dict(os.environ)
        env["NODE_ENV"] = "production"
        env["WEIBO_CLI_TOKEN"] = token
        returncode, stdout, stderr = await _run_process(
            self._settings, path, arguments, env
        )
        if returncode != 0:
            _raise_cli_error(stderr.decode("utf-8", errors="replace"))
        try:
            payload = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamError("official CLI returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise UpstreamError("official CLI JSON root must be an object")
        return payload

    def _validate_legacy_state(self) -> None:
        if self._legacy_state.get("status") not in {"available", "rate_limited"}:
            raise AtomicJsonError(f"invalid legacy extend state: {self._legacy_path}")
        if _parse_time(self._legacy_state.get("blocked_until")) is None and self._legacy_state.get(
            "blocked_until"
        ) not in {None, ""}:
            raise AtomicJsonError(f"invalid legacy extend blocked_until: {self._legacy_path}")

    def _save_legacy_state(self) -> None:
        if not self._read_only:
            atomic_write_json(self._legacy_path, self._legacy_state)


def _status_uid(status: dict[str, Any]) -> str:
    user = status.get("user") or {}
    if not isinstance(user, dict):
        return ""
    return str(user.get("idstr") or user.get("id") or "")


async def _get_access_token(settings: Settings, path: str) -> str:
    cached = _TOKEN_CACHE.get(path)
    if cached:
        return cached
    lock = _TOKEN_LOCKS.setdefault(path, asyncio.Lock())
    async with lock:
        cached = _TOKEN_CACHE.get(path)
        if cached:
            return cached
        env = dict(os.environ)
        env["NODE_ENV"] = "production"
        env.pop("WEIBO_CLI_TOKEN", None)
        env.pop("WEIBO_TOKEN", None)
        returncode, stdout, stderr = await _run_process(
            settings, path, ["auth", "token", "--export"], env
        )
        if returncode != 0:
            _raise_cli_error(stderr.decode("utf-8", errors="replace"))
        token = stdout.decode("utf-8", errors="strict").strip()
        if not token or any(char.isspace() for char in token) or len(token) > 4096:
            raise AuthenticationError("official CLI returned an invalid access token")
        _TOKEN_CACHE[path] = token
        return token


async def _run_process(
    settings: Settings,
    path: str,
    arguments: list[str],
    env: dict[str, str],
) -> tuple[int, bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            path,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=settings.weibo_cli_timeout
        )
    except TimeoutError as exc:
        if "process" in locals():
            process.kill()
            await process.wait()
        raise UpstreamError("official CLI timed out") from exc
    except OSError as exc:
        raise SourceConfigurationError("cannot start official CLI") from exc
    max_bytes = settings.weibo_cli_max_output_bytes
    if len(stdout) > max_bytes or len(stderr) > max_bytes:
        raise UpstreamError("official CLI output exceeded configured limit")
    return process.returncode, stdout, stderr


def _raise_cli_error(stderr: str) -> None:
    lowered = " ".join(stderr.lower().split())
    code_match = re.search(r"\[([A-Z][A-Z0-9_]+)\]", stderr)
    code = code_match.group(1) if code_match else "CLI_ERROR"
    if any(marker in lowered for marker in _AUTH_MARKERS):
        raise AuthenticationError(f"official CLI authentication failed: code={code}")
    if any(marker in lowered for marker in _RATE_MARKERS):
        raise RateLimitedError(
            f"official CLI quota/rate limit: code={code}", status_code=429
        )
    if any(marker in lowered for marker in _CONFIG_MARKERS):
        raise SourceConfigurationError(f"official CLI configuration failed: code={code}")
    raise UpstreamError(f"official CLI request failed: code={code}")
