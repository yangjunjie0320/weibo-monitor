import asyncio
from unittest.mock import AsyncMock

import yaml

import main
from src.config import Settings
from src.weibo import RateLimitedError


def _settings(tmp_path) -> Settings:
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text(
        yaml.safe_dump({"accounts": [{"name": "测试", "uid": "42"}]}),
        encoding="utf-8",
    )
    return Settings(
        accounts_file=str(accounts),
        state_file=str(tmp_path / "seen.json"),
        health_file=str(tmp_path / "health.json"),
        weibo_cookie_file=str(tmp_path / "cookie.txt"),
        forward_enabled=False,
        weibo_source="mobile",
    )


async def test_probe_reads_one_account_without_writing_state(tmp_path, monkeypatch):
    class FakeWeibo:
        def __init__(self, settings, http_client):
            pass

        async def ensure_cookie(self):
            pass

        async def timeline_page(self, uid, page):
            assert (uid, page) == ("42", 1)
            return {"ok": 1, "data": {"cards": []}}

    monkeypatch.setattr(main, "WeiboClient", FakeWeibo)
    settings = _settings(tmp_path)

    result = await main._probe(settings, None)

    assert result == 0
    assert not (tmp_path / "seen.json").exists()
    assert not (tmp_path / "health.json").exists()


async def test_probe_uses_exit_code_two_for_rate_limit(tmp_path, monkeypatch):
    class LimitedWeibo:
        def __init__(self, settings, http_client):
            pass

        async def ensure_cookie(self):
            pass

        async def timeline_page(self, uid, page):
            raise RateLimitedError("HTTP 432", status_code=432)

    monkeypatch.setattr(main, "WeiboClient", LimitedWeibo)

    assert await main._probe(_settings(tmp_path), "42") == 2


async def test_official_probe_uses_count_one_and_writes_no_state(tmp_path, monkeypatch):
    class FakeOfficial:
        def __init__(self, settings, read_only=False):
            assert read_only

        async def probe(self, uid):
            assert uid == "42"
            return {"ok": 1, "statuses": []}

    monkeypatch.setattr(main, "OfficialCliClient", FakeOfficial)
    configured = _settings(tmp_path)
    configured.weibo_source = "official_cli"

    assert await main._probe(configured, None) == 0
    assert not (tmp_path / "seen.json").exists()
    assert not (tmp_path / "health.json").exists()


async def test_source_check_does_not_probe_content(tmp_path, monkeypatch):
    source_check = AsyncMock()

    class FakeOfficial:
        def __init__(self, settings, read_only=False):
            assert read_only

        async def source_check(self):
            await source_check()

    monkeypatch.setattr(main, "OfficialCliClient", FakeOfficial)
    configured = _settings(tmp_path)
    configured.weibo_source = "official_cli"

    assert await main._source_check(configured) == 0
    source_check.assert_awaited_once()


async def test_essential_side_task_failure_stops_supervisor():
    class FakeMonitor:
        async def run_forever(self):
            await asyncio.Future()

    async def fail():
        raise RuntimeError("listener died")

    side_task = asyncio.create_task(fail(), name="listener")
    try:
        await main._run_supervised(FakeMonitor(), [side_task])
    except RuntimeError as exc:
        assert "essential task died: listener" in str(exc)
    else:
        raise AssertionError("supervisor should propagate essential task failure")
