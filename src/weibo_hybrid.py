"""混合抓取源：mobile 为主，被限流期间自动切 official CLI，恢复自动切回。

设计见 docs/hybrid-source-design.md。熔断状态持久化到 hybrid_state_file，
重启不重置；退避按"上次封锁结束后短时间内再次被限流"指数升级。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

from .atomic_json import AtomicJsonError, atomic_write_json, load_json_object
from .config import Settings
from .models import Account
from .weibo import RateLimitedError, WeiboClient
from .weibo_cli import OfficialCliClient

logger = logging.getLogger(__name__)

_UTC = dt.UTC


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


class HybridClient:
    """mobile 主源 + official CLI 兜底，对 monitor 暴露统一的客户端接口。"""

    def __init__(
        self,
        settings: Settings,
        mobile_client: WeiboClient,
        cli_client: OfficialCliClient,
        *,
        read_only: bool = False,
    ) -> None:
        self._settings = settings
        self._mobile = mobile_client
        self._cli = cli_client
        self._read_only = read_only
        self._accounts: list[Account] = []
        self._active = "mobile"
        self.last_cycle_requests = 0
        self._state_path = Path(settings.hybrid_state_file)
        self._state: dict[str, Any] = {
            "schema_version": 1,
            "status": "available",
            "updated_at": None,
            "blocked_until": None,
            "backoff_seconds": None,
            "last_error": None,
        }
        if self._state_path.exists():
            self._state = load_json_object(self._state_path)
            self._validate_state()

    @property
    def active_source(self) -> str:
        return self._active

    @property
    def requires_account_delay(self) -> bool:
        return self._active == "mobile"

    async def ensure_cookie(self) -> None:
        await self._mobile.ensure_cookie()

    def reload_static_cookie(self) -> None:
        # CLI 的 legacy 客户端就是同一个 mobile 实例，重载一次即可
        self._mobile.reload_static_cookie()

    async def prepare_cycle(self, accounts: list[Account]) -> None:
        self._accounts = list(accounts)
        self.last_cycle_requests = 0
        if self._blocked_now():
            if self._active != "cli":
                logger.info(
                    "hybrid: mobile blocked until %s, cycle uses official CLI",
                    self._state.get("blocked_until"),
                )
            self._active = "cli"
            await self._cli.prepare_cycle(accounts)
            self.last_cycle_requests = self._cli.last_cycle_requests
        else:
            if self._active != "mobile":
                logger.info("hybrid: mobile block expired, switching back to mobile")
            self._active = "mobile"

    async def timeline_page(self, uid: str, page: int) -> dict[str, Any]:
        if self._active == "cli":
            return await self._cli.timeline_page(uid, page)
        try:
            return await self._mobile.timeline_page(uid, page)
        except RateLimitedError as exc:
            self._block_mobile(exc)
            # 轮中无缝切换：为全部账号批量预取后当轮剩余请求走 CLI。
            # CLI 也失败（限流/额度耗尽）则异常上抛，交给 monitor 全局熔断。
            self._active = "cli"
            await self._cli.prepare_cycle(self._accounts)
            self.last_cycle_requests += self._cli.last_cycle_requests
            return await self._cli.timeline_page(uid, page)

    async def fetch_extend(self, mid: str) -> dict[str, Any]:
        # 长文展开始终走 CLI 包装的 legacy 路径（自带独立熔断）
        return await self._cli.fetch_extend(mid)

    def _blocked_now(self) -> bool:
        blocked_until = _parse_time(self._state.get("blocked_until"))
        return bool(blocked_until and blocked_until > _now())

    def _block_mobile(self, exc: RateLimitedError) -> None:
        now = _now()
        backoff = self._settings.hybrid_block_initial_seconds
        previous_until = _parse_time(self._state.get("blocked_until"))
        previous_backoff = self._state.get("backoff_seconds")
        # 上次封锁结束后很快又被限流 → 指数升级；平稳运行过一段则重新起步
        rapid_relapse = (
            previous_until is not None
            and isinstance(previous_backoff, int)
            and (now - previous_until).total_seconds()
            < 2 * self._settings.poll_interval_seconds
        )
        if rapid_relapse:
            backoff = min(previous_backoff * 2, self._settings.hybrid_block_max_seconds)
        blocked_until = now + dt.timedelta(seconds=backoff)
        self._state.update(
            status="blocked",
            updated_at=_iso(now),
            blocked_until=_iso(blocked_until),
            backoff_seconds=backoff,
            last_error={"kind": "rate_limited", "message": str(exc)},
        )
        self._save_state()
        logger.warning(
            "hybrid: mobile rate limited (HTTP %s), failing over to official CLI "
            "until %s (backoff=%ds)",
            exc.status_code or "-",
            _iso(blocked_until),
            backoff,
        )

    def _validate_state(self) -> None:
        if self._state.get("status") not in {"available", "blocked"}:
            raise AtomicJsonError(f"invalid hybrid source state: {self._state_path}")
        blocked_until = self._state.get("blocked_until")
        if _parse_time(blocked_until) is None and blocked_until not in {None, ""}:
            raise AtomicJsonError(f"invalid hybrid source state: {self._state_path}")

    def _save_state(self) -> None:
        if not self._read_only:
            atomic_write_json(self._state_path, self._state)
