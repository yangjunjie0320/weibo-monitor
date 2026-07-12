import datetime as dt
import json
from unittest.mock import AsyncMock, MagicMock

from src.config import Settings
from src.models import Account
from src.weibo import RateLimitedError
from src.weibo_hybrid import HybridClient

ACCOUNTS = [Account(name="甲", uid="1"), Account(name="乙", uid="2")]


def settings(tmp_path, **overrides) -> Settings:
    values = {
        "forward_enabled": False,
        "weibo_source": "hybrid",
        "hybrid_state_file": str(tmp_path / "hybrid.json"),
        "poll_interval_seconds": 3600,
        "hybrid_block_initial_seconds": 1800,
        "hybrid_block_max_seconds": 43200,
    }
    values.update(overrides)
    return Settings(**values)


def make_client(tmp_path, *, mobile=None, cli=None, read_only=False, **overrides):
    mobile = mobile if mobile is not None else AsyncMock()
    cli = cli if cli is not None else AsyncMock()
    cli.last_cycle_requests = 2
    client = HybridClient(
        settings(tmp_path, **overrides), mobile, cli, read_only=read_only
    )
    return client, mobile, cli


def write_state(tmp_path, *, blocked_until: dt.datetime | None, backoff: int | None):
    (tmp_path / "hybrid.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "blocked" if blocked_until else "available",
                "updated_at": None,
                "blocked_until": blocked_until.isoformat(timespec="seconds")
                if blocked_until
                else None,
                "backoff_seconds": backoff,
                "last_error": None,
            }
        ),
        encoding="utf-8",
    )


async def test_mobile_is_primary_when_not_blocked(tmp_path):
    client, mobile, cli = make_client(tmp_path)
    mobile.timeline_page.return_value = {"ok": 1, "data": {"cards": []}}

    await client.prepare_cycle(ACCOUNTS)
    assert client.active_source == "mobile"
    assert client.requires_account_delay
    assert client.last_cycle_requests == 0

    result = await client.timeline_page("1", 1)
    assert result == {"ok": 1, "data": {"cards": []}}
    cli.prepare_cycle.assert_not_awaited()
    cli.timeline_page.assert_not_awaited()


async def test_rate_limit_fails_over_to_cli_mid_cycle(tmp_path):
    client, mobile, cli = make_client(tmp_path)
    mobile.timeline_page.side_effect = RateLimitedError("HTTP 432", status_code=432)
    cli.timeline_page.return_value = {"ok": 1, "statuses": []}

    await client.prepare_cycle(ACCOUNTS)
    result = await client.timeline_page("1", 1)

    assert result == {"ok": 1, "statuses": []}
    assert client.active_source == "cli"
    assert not client.requires_account_delay
    cli.prepare_cycle.assert_awaited_once_with(ACCOUNTS)
    assert client.last_cycle_requests == 2

    state = json.loads((tmp_path / "hybrid.json").read_text())
    assert state["status"] == "blocked"
    assert state["backoff_seconds"] == 1800
    assert state["blocked_until"]


async def test_persisted_block_survives_restart(tmp_path):
    write_state(
        tmp_path,
        blocked_until=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        backoff=1800,
    )
    client, mobile, cli = make_client(tmp_path)

    await client.prepare_cycle(ACCOUNTS)
    assert client.active_source == "cli"
    cli.prepare_cycle.assert_awaited_once_with(ACCOUNTS)
    assert client.last_cycle_requests == 2

    await client.timeline_page("1", 1)
    mobile.timeline_page.assert_not_awaited()


async def test_recovers_to_mobile_after_block_expires(tmp_path):
    write_state(
        tmp_path,
        blocked_until=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5),
        backoff=1800,
    )
    client, _mobile, cli = make_client(tmp_path)

    await client.prepare_cycle(ACCOUNTS)
    assert client.active_source == "mobile"
    cli.prepare_cycle.assert_not_awaited()


async def test_backoff_doubles_on_rapid_relapse(tmp_path):
    # 上一次封锁 5 分钟前刚结束（< 2 个轮询周期）→ 退避翻倍
    write_state(
        tmp_path,
        blocked_until=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5),
        backoff=1800,
    )
    client, mobile, cli = make_client(tmp_path)
    mobile.timeline_page.side_effect = RateLimitedError("HTTP 432", status_code=432)
    cli.timeline_page.return_value = {"ok": 1, "statuses": []}

    await client.prepare_cycle(ACCOUNTS)
    await client.timeline_page("1", 1)

    state = json.loads((tmp_path / "hybrid.json").read_text())
    assert state["backoff_seconds"] == 3600


async def test_backoff_caps_at_max(tmp_path):
    write_state(
        tmp_path,
        blocked_until=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5),
        backoff=43200,
    )
    client, mobile, cli = make_client(tmp_path)
    mobile.timeline_page.side_effect = RateLimitedError("HTTP 432", status_code=432)
    cli.timeline_page.return_value = {"ok": 1, "statuses": []}

    await client.prepare_cycle(ACCOUNTS)
    await client.timeline_page("1", 1)

    state = json.loads((tmp_path / "hybrid.json").read_text())
    assert state["backoff_seconds"] == 43200


async def test_backoff_resets_after_quiet_period(tmp_path):
    # 上一次封锁很久以前结束（> 2 个轮询周期）→ 退避重新起步
    write_state(
        tmp_path,
        blocked_until=dt.datetime.now(dt.UTC) - dt.timedelta(hours=5),
        backoff=43200,
    )
    client, mobile, cli = make_client(tmp_path)
    mobile.timeline_page.side_effect = RateLimitedError("HTTP 432", status_code=432)
    cli.timeline_page.return_value = {"ok": 1, "statuses": []}

    await client.prepare_cycle(ACCOUNTS)
    await client.timeline_page("1", 1)

    state = json.loads((tmp_path / "hybrid.json").read_text())
    assert state["backoff_seconds"] == 1800


async def test_cli_failure_during_failover_propagates(tmp_path):
    client, mobile, cli = make_client(tmp_path)
    mobile.timeline_page.side_effect = RateLimitedError("HTTP 432", status_code=432)
    cli.prepare_cycle.side_effect = RateLimitedError("credits exhausted")

    await client.prepare_cycle(ACCOUNTS)
    try:
        await client.timeline_page("1", 1)
    except RateLimitedError as exc:
        assert "credits" in str(exc)
    else:
        raise AssertionError("expected RateLimitedError")


async def test_fetch_extend_delegates_to_cli(tmp_path):
    client, _mobile, cli = make_client(tmp_path)
    cli.fetch_extend.return_value = {"longTextContent": "长文"}

    assert await client.fetch_extend("9") == {"longTextContent": "长文"}
    cli.fetch_extend.assert_awaited_once_with("9")


async def test_read_only_does_not_persist_state(tmp_path):
    client, mobile, cli = make_client(tmp_path, read_only=True)
    mobile.timeline_page.side_effect = RateLimitedError("HTTP 432", status_code=432)
    cli.timeline_page.return_value = {"ok": 1, "statuses": []}

    await client.prepare_cycle(ACCOUNTS)
    await client.timeline_page("1", 1)

    assert not (tmp_path / "hybrid.json").exists()


def test_reload_static_cookie_reloads_mobile_once(tmp_path):
    mobile = MagicMock()
    client, _, _ = make_client(tmp_path, mobile=mobile)
    client.reload_static_cookie()
    mobile.reload_static_cookie.assert_called_once()
