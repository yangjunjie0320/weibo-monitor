from __future__ import annotations

import datetime as dt
from copy import deepcopy
from pathlib import Path
from typing import Any

from .atomic_json import AtomicJsonError, atomic_write_json, load_json_object

UTC = dt.UTC
_STATUSES = {"starting", "healthy", "degraded", "rate_limited", "failed"}


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(value: dt.datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def parse_iso(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def empty_cycle(accounts_total: int = 0) -> dict[str, int | bool]:
    return {
        "accounts_total": accounts_total,
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "new": 0,
        "pushed": 0,
        "dropped": 0,
        "rate_limited": False,
        "source_requests": 0,
    }


class HealthStore:
    """持久化运行健康状态与跨重启限流熔断。"""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self._path = Path(path)
        self._read_only = read_only
        if self._path.exists():
            self._data = load_json_object(self._path)
            self._validate()
        else:
            self._data: dict[str, Any] = {
                "schema_version": 1,
                "status": "starting",
                "updated_at": iso(utc_now()),
                "last_healthy_cycle_at": None,
                "next_cycle_at": None,
                "blocked_until": None,
                "rate_limited_streak": 0,
                "cycle": empty_cycle(),
                "last_error": None,
            }

    def _validate(self) -> None:
        status = self._data.get("status")
        if status not in _STATUSES:
            raise AtomicJsonError(f"invalid health status in {self._path}: {status!r}")
        if not isinstance(self._data.get("cycle"), dict):
            raise AtomicJsonError(f"health cycle must be an object: {self._path}")
        streak = self._data.get("rate_limited_streak", 0)
        if not isinstance(streak, int) or streak < 0:
            raise AtomicJsonError(f"invalid health rate_limited_streak: {self._path}")
        last_error = self._data.get("last_error")
        if last_error is not None and not isinstance(last_error, dict):
            raise AtomicJsonError(f"health last_error must be an object: {self._path}")

    @property
    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self._data)

    @property
    def blocked_until(self) -> dt.datetime | None:
        return parse_iso(self._data.get("blocked_until"))

    @property
    def rate_limited_streak(self) -> int:
        value = self._data.get("rate_limited_streak", 0)
        return value if isinstance(value, int) and value >= 0 else 0

    def write(
        self,
        *,
        status: str | None = None,
        cycle: dict[str, Any] | None = None,
        next_cycle_at: dt.datetime | None | object = ...,
        blocked_until: dt.datetime | None | object = ...,
        rate_limited_streak: int | None = None,
        last_error: dict[str, str] | None | object = ...,
        mark_healthy: bool = False,
    ) -> None:
        now = utc_now()
        self._data["schema_version"] = 1
        self._data["updated_at"] = iso(now)
        if status is not None:
            if status not in _STATUSES:
                raise ValueError(f"invalid health status: {status}")
            self._data["status"] = status
        if cycle is not None:
            self._data["cycle"] = deepcopy(cycle)
        if next_cycle_at is not ...:
            self._data["next_cycle_at"] = iso(next_cycle_at)
        if blocked_until is not ...:
            self._data["blocked_until"] = iso(blocked_until)
        if rate_limited_streak is not None:
            self._data["rate_limited_streak"] = rate_limited_streak
        if last_error is not ...:
            self._data["last_error"] = last_error
        if mark_healthy:
            self._data["last_healthy_cycle_at"] = iso(now)
        if not self._read_only:
            atomic_write_json(self._path, self._data)

    def mark_starting(self, accounts_total: int) -> None:
        self.write(status="starting", cycle=empty_cycle(accounts_total), last_error=None)
