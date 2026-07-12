import datetime as dt
import json
import os
from unittest.mock import AsyncMock

import pytest

from src.config import Settings
from src.models import Account
from src.weibo import (
    AuthenticationError,
    RateLimitedError,
    SourceConfigurationError,
    UpstreamError,
    extract_mblogs,
    parse_post,
)
from src.weibo_cli import (
    OfficialCliClient,
    _raise_cli_error,
    balanced_account_groups,
    check_cli_install,
)


def settings(tmp_path, **overrides) -> Settings:
    values = {
        "forward_enabled": False,
        "weibo_source": "official_cli",
        "weibo_cli_path": str(tmp_path / "weibo"),
        "legacy_extend_state_file": str(tmp_path / "legacy.json"),
        "legacy_extend_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def accounts(count: int = 34) -> list[Account]:
    return [Account(name=f"account-{index}", uid=str(1000 + index)) for index in range(count)]


def status(uid: str, mid: str = "9") -> dict:
    return {
        "idstr": mid,
        "mid": mid,
        "created_at": "Tue Jul 07 12:30:00 +0800 2026",
        "text": "hello",
        "user": {"idstr": uid, "screen_name": f"u-{uid}"},
    }


async def test_34_accounts_use_two_balanced_cli_calls_and_no_page_two(tmp_path):
    client = OfficialCliClient(settings(tmp_path))
    calls: list[list[str]] = []

    async def invoke(arguments):
        calls.append(arguments)
        uids = arguments[arguments.index("--uids") + 1].split(",")
        return {"statuses": [status(uid, str(index + 1)) for index, uid in enumerate(uids)]}

    client._invoke_json = invoke
    configured = accounts()

    await client.prepare_cycle(configured)

    assert len(calls) == 2
    assert client.last_cycle_requests == 2
    first = calls[0][calls[0].index("--uids") + 1].split(",")
    second = calls[1][calls[1].index("--uids") + 1].split(",")
    assert first == [item.uid for item in configured[::2]]
    assert second == [item.uid for item in configured[1::2]]
    assert all(call[call.index("--count") + 1] == "5" for call in calls)
    assert len(extract_mblogs(await client.timeline_page(configured[0].uid, 1))) == 1
    assert extract_mblogs(await client.timeline_page(configured[0].uid, 2)) == []
    assert len(calls) == 2


def test_balanced_groups_respect_twenty_user_limit():
    groups = balanced_account_groups(accounts(), 20)
    assert [len(group) for group in groups] == [17, 17]


async def test_probe_uses_one_result(tmp_path):
    client = OfficialCliClient(settings(tmp_path))
    invoke = AsyncMock(return_value={"statuses": [status("42")]})
    client._invoke_json = invoke

    result = await client.probe("42")

    assert len(result["statuses"]) == 1
    arguments = invoke.await_args.args[0]
    assert arguments[arguments.index("--count") + 1] == "1"


async def test_source_check_requires_ready_account_and_allowed_batch(tmp_path):
    client = OfficialCliClient(settings(tmp_path))
    client._invoke_json = AsyncMock(
        side_effect=[
            {"ready": True},
            {"commands": [{"action": "user_timeline_batch", "access": "allowed"}]},
        ]
    )

    await client.source_check()

    assert client._invoke_json.await_count == 2


async def test_source_check_rejects_missing_subscription(tmp_path):
    client = OfficialCliClient(settings(tmp_path))
    client._invoke_json = AsyncMock(return_value={"ready": False})

    with pytest.raises(SourceConfigurationError, match="not ready"):
        await client.source_check()


@pytest.mark.parametrize(
    ("message", "error_type"),
    [
        ("登录已失效 [UNAUTHORIZED]", AuthenticationError),
        ("额度不足 [TOO_MANY_REQUESTS]", RateLimitedError),
        ("plan locked [SUBSCRIPTION_PLAN_NOT_ALLOWED]", SourceConfigurationError),
        ("system error [WEIBO_10001]", UpstreamError),
    ],
)
def test_cli_errors_are_classified_without_exposing_body(message, error_type):
    with pytest.raises(error_type) as caught:
        _raise_cli_error(message + " secret-body")
    assert "secret-body" not in str(caught.value)


class FakeProcess:
    def __init__(self, stdout=b"{}", stderr=b"", returncode=0, *, hangs=False):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.hangs = hangs
        self.killed = False

    async def communicate(self):
        if self.hangs:
            await __import__("asyncio").Future()
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def executable(tmp_path):
    path = tmp_path / "weibo"
    path.write_text("#!/bin/sh\necho weibo-cli 0.8.3\n", encoding="utf-8")
    os.chmod(path, 0o700)
    return path


async def test_invalid_json_and_output_limit_are_upstream_errors(tmp_path, monkeypatch):
    path = executable(tmp_path)
    client = OfficialCliClient(settings(tmp_path, weibo_cli_path=str(path)))

    monkeypatch.setattr(
        "src.weibo_cli.asyncio.create_subprocess_exec",
        AsyncMock(return_value=FakeProcess(stdout=b"not-json")),
    )
    with pytest.raises(UpstreamError, match="invalid JSON"):
        await client._invoke_json(["doctor", "--output", "json"])

    monkeypatch.setattr(
        "src.weibo_cli.asyncio.create_subprocess_exec",
        AsyncMock(return_value=FakeProcess(stdout=b"x" * 11)),
    )
    client._settings = settings(
        tmp_path, weibo_cli_path=str(path), weibo_cli_max_output_bytes=10
    )
    with pytest.raises(UpstreamError, match="exceeded"):
        await client._invoke_json(["doctor", "--output", "json"])


async def test_subprocess_timeout_kills_cli(tmp_path, monkeypatch):
    path = executable(tmp_path)
    process = FakeProcess(hangs=True)
    monkeypatch.setattr(
        "src.weibo_cli.asyncio.create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    client = OfficialCliClient(
        settings(tmp_path, weibo_cli_path=str(path), weibo_cli_timeout=0.001)
    )

    with pytest.raises(UpstreamError, match="timed out"):
        await client._invoke_json(["doctor"])
    assert process.killed


async def test_access_token_is_exported_once_and_kept_out_of_arguments(tmp_path, monkeypatch):
    path = executable(tmp_path)
    catalog = {"commands": [{"action": "user_timeline_batch", "access": "allowed"}]}
    creator = AsyncMock(
        side_effect=[
            FakeProcess(stdout=b"at_secret\n"),
            FakeProcess(stdout=b'{"ready": true}'),
            FakeProcess(stdout=json.dumps(catalog).encode()),
            FakeProcess(stdout=b'{"statuses": []}'),
        ]
    )
    monkeypatch.setattr("src.weibo_cli.asyncio.create_subprocess_exec", creator)
    client = OfficialCliClient(settings(tmp_path, weibo_cli_path=str(path)))

    await client.source_check()
    await client.probe("42")

    assert creator.await_count == 4
    assert creator.await_args_list[0].args[1:4] == ("auth", "token", "--export")
    for call in creator.await_args_list[1:]:
        assert "at_secret" not in call.args
        assert call.kwargs["env"]["WEIBO_CLI_TOKEN"] == "at_secret"


async def test_unauthorized_command_refreshes_token_once(tmp_path, monkeypatch):
    path = executable(tmp_path)
    creator = AsyncMock(
        side_effect=[
            FakeProcess(stdout=b"at_first\n"),
            FakeProcess(stderr=b"login expired [UNAUTHORIZED]", returncode=1),
            FakeProcess(stdout=b"at_second\n"),
            FakeProcess(stdout=b'{"statuses": []}'),
        ]
    )
    monkeypatch.setattr("src.weibo_cli.asyncio.create_subprocess_exec", creator)
    client = OfficialCliClient(settings(tmp_path, weibo_cli_path=str(path)))

    assert await client.probe("42") == {"ok": 1, "statuses": []}
    assert creator.await_count == 4


def test_offline_install_check_requires_pinned_version(tmp_path):
    path = executable(tmp_path)
    check_cli_install(settings(tmp_path, weibo_cli_path=str(path)))
    with pytest.raises(SourceConfigurationError, match="version mismatch"):
        check_cli_install(
            settings(
                tmp_path,
                weibo_cli_path=str(path),
                weibo_cli_version="9.9.9",
            )
        )


async def test_legacy_rate_limit_is_persisted_and_does_not_propagate(tmp_path):
    legacy = AsyncMock()
    legacy.fetch_extend_strict.side_effect = RateLimitedError("HTTP 432", status_code=432)
    configured = settings(
        tmp_path,
        legacy_extend_enabled=True,
        legacy_extend_cooldown_seconds=43200,
    )
    first = OfficialCliClient(configured, legacy_client=legacy)

    assert await first.fetch_extend("9") == {}
    state = json.loads((tmp_path / "legacy.json").read_text())
    assert state["status"] == "rate_limited"
    assert dt.datetime.fromisoformat(state["blocked_until"]) > dt.datetime.now(dt.UTC)

    after_restart = AsyncMock()
    second = OfficialCliClient(configured, legacy_client=after_restart)
    assert await second.fetch_extend("9") == {}
    after_restart.fetch_extend_strict.assert_not_awaited()


def test_reload_static_cookie_delegates_to_legacy_client(tmp_path):
    legacy = AsyncMock()
    client = OfficialCliClient(
        settings(tmp_path, legacy_extend_enabled=True), legacy_client=legacy
    )
    client.reload_static_cookie()
    legacy.reload_static_cookie.assert_called_once()

    without_legacy = OfficialCliClient(settings(tmp_path))
    without_legacy.reload_static_cookie()  # 不应抛异常


async def test_legacy_circuit_is_read_only_in_dry_run(tmp_path):
    legacy = AsyncMock()
    legacy.fetch_extend_strict.side_effect = RateLimitedError("HTTP 432", status_code=432)
    client = OfficialCliClient(
        settings(tmp_path, legacy_extend_enabled=True),
        legacy_client=legacy,
        read_only=True,
    )

    assert await client.fetch_extend("9") == {}
    assert not (tmp_path / "legacy.json").exists()


async def test_legacy_upstream_failure_also_opens_optional_circuit(tmp_path):
    legacy = AsyncMock()
    legacy.fetch_extend_strict.side_effect = UpstreamError("invalid HTML")
    client = OfficialCliClient(
        settings(tmp_path, legacy_extend_enabled=True), legacy_client=legacy
    )

    assert await client.fetch_extend("9") == {}
    state = json.loads((tmp_path / "legacy.json").read_text())
    assert state["last_error"] == {"kind": "upstream", "message": "UpstreamError"}


def test_official_status_parsing_supports_images_truncation_and_detail_url():
    raw = status("42", "123456789")
    raw.update(
        isLongText=True,
        truncated=True,
        pic_ids=["abc", "def"],
    )
    account = Account(name="fallback", uid="42")

    [(card, mblog)] = extract_mblogs({"statuses": [raw]})
    post = parse_post(account, card, mblog)

    assert post is not None
    assert post.text_truncated
    assert post.url == "https://m.weibo.cn/detail/123456789"
    assert post.image_urls == [
        "https://wx1.sinaimg.cn/large/abc.jpg",
        "https://wx1.sinaimg.cn/large/def.jpg",
    ]
