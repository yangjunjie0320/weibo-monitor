import datetime as dt
from unittest.mock import AsyncMock

import pytest

from src.config import Settings
from src.health import HealthStore, empty_cycle, utc_now
from src.monitor import Monitor


def test_health_roundtrip_preserves_circuit_breaker(tmp_path):
    path = tmp_path / "health.json"
    blocked_until = utc_now() + dt.timedelta(hours=2)
    store = HealthStore(path)
    store.write(
        status="rate_limited",
        cycle={**empty_cycle(34), "attempted": 1, "failed": 1, "rate_limited": True},
        next_cycle_at=blocked_until,
        blocked_until=blocked_until,
        rate_limited_streak=3,
        last_error={"kind": "rate_limited", "message": "HTTP 432"},
    )

    reloaded = HealthStore(path)

    assert reloaded.snapshot["status"] == "rate_limited"
    assert reloaded.rate_limited_streak == 3
    assert reloaded.blocked_until == blocked_until.replace(microsecond=0)
    assert path.stat().st_mode & 0o777 == 0o600


def test_read_only_health_never_creates_file(tmp_path):
    path = tmp_path / "health.json"
    store = HealthStore(path, read_only=True)
    store.mark_starting(34)

    assert not path.exists()


async def test_monitor_persists_rate_limit_backoff_across_restart(tmp_path, monkeypatch):
    class StopLoop(Exception):
        pass

    path = tmp_path / "health.json"
    health = HealthStore(path)
    settings = Settings(
        forward_enabled=False,
        rate_limit_rest_seconds=1800,
        rate_limit_rest_max_seconds=43200,
        rate_limit_jitter_ratio=0,
        poll_interval_seconds=600,
    )
    monitor = Monitor(settings, None, None, None, [object()], health)
    monitor.run_cycle = AsyncMock(
        return_value={
            **empty_cycle(1),
            "attempted": 1,
            "failed": 1,
            "rate_limited": True,
            "last_error": {"kind": "rate_limited", "message": "HTTP 432"},
        }
    )

    delays: list[float] = []

    async def stop_after_write(delay: float) -> None:
        delays.append(delay)
        raise StopLoop

    monkeypatch.setattr("src.monitor.asyncio.sleep", stop_after_write)
    with pytest.raises(StopLoop):
        await monitor.run_forever()

    persisted = HealthStore(path)
    assert persisted.snapshot["status"] == "rate_limited"
    assert persisted.rate_limited_streak == 1
    assert 1790 <= delays[0] <= 1810

    restarted = Monitor(settings, None, None, None, [object()], persisted)
    restarted.run_cycle = AsyncMock(side_effect=AssertionError("blocked cycle must not run"))
    delays.clear()
    with pytest.raises(StopLoop):
        await restarted.run_forever()
    assert 1780 <= delays[0] <= 1810
