import json
from types import SimpleNamespace

import pytest

from src.bitable import BitableError, BitableSyncer, _parse_url_token, _to_ms
from src.config import Settings
from src.forward import ForwardStore


def test_parse_url_token():
    assert _parse_url_token("https://x.feishu.cn/base/AbC123?table=t") == (
        "base",
        "AbC123",
    )
    assert _parse_url_token(
        "https://skyland2020.feishu.cn/wiki/DydSwixm1i2uRTkXIFucEDMAnkc?from=from_copylink"
    ) == ("wiki", "DydSwixm1i2uRTkXIFucEDMAnkc")
    with pytest.raises(BitableError):
        _parse_url_token("https://x.feishu.cn/docs/xxx")


def test_to_ms():
    assert _to_ms("2026-07-01T12:30:00+08:00") == 1782880200000
    assert _to_ms("") is None
    assert _to_ms("not-a-date") is None


def test_to_fields(tmp_path):
    syncer = BitableSyncer(Settings(), None, ForwardStore(tmp_path / "f.json"))
    rec = {
        "screen_name": "测试博主",
        "label": "市场数据",
        "summary": "今天试驾了一台新车",
        "url": "https://weibo.com/42/Babc",
        "forwarder_open_id": "ou_laoma",
        "post_created_at": "2026-07-01T12:30:00+08:00",
        "forwarded_at": "2026-07-01T05:00:00+00:00",
    }
    fields = syncer._to_fields("m1", rec)
    assert fields["博主"] == "测试博主"
    assert fields["原帖链接"] == {
        "text": "原帖",
        "link": "https://weibo.com/42/Babc",
    }
    assert fields["转发人"] == "ou_laoma"
    assert isinstance(fields["发帖时间"], int)
    assert isinstance(fields["转发时间"], int)
    assert fields["mid"] == "m1"


def _legacy_store(tmp_path, count):
    path = tmp_path / "forwarded.json"
    path.write_text(
        json.dumps(
            {
                "forwards": {
                    f"m{i}": {
                        "screen_name": f"博主{i}",
                        "forwarded_at": "2026-07-01T05:00:00+00:00",
                        "synced": False,
                    }
                    for i in range(count)
                }
            }
        ),
        encoding="utf-8",
    )
    return ForwardStore(path)


def test_sync_deduplicates_remote_mids_and_batches_at_100(tmp_path, monkeypatch):
    store = _legacy_store(tmp_path, 205)
    syncer = BitableSyncer(Settings(), None, store)
    monkeypatch.setattr(syncer, "_ensure_table", lambda: None)
    monkeypatch.setattr(syncer, "_remote_has_mid", lambda mid: mid in {"m0", "m204"})
    batches = []
    monkeypatch.setattr(syncer, "_batch_create", lambda batch: batches.append(list(batch)))

    assert syncer._sync_once() == 203
    assert [len(batch) for batch in batches] == [100, 100, 3]
    assert all(rec["archive_status"] == "synced" for rec in store.records().values())


def test_batch_failure_preserves_local_pending_state(tmp_path, monkeypatch):
    store = _legacy_store(tmp_path, 101)
    syncer = BitableSyncer(Settings(bitable_sync_interval_seconds=60), None, store)
    monkeypatch.setattr(syncer, "_ensure_table", lambda: None)
    monkeypatch.setattr(syncer, "_remote_has_mid", lambda mid: False)
    calls = 0

    def create(batch):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise BitableError("remote failed")

    monkeypatch.setattr(syncer, "_batch_create", create)

    with pytest.raises(BitableError, match="remote failed"):
        syncer._sync_once()

    records = store.records()
    assert all(records[f"m{i}"]["archive_status"] == "synced" for i in range(100))
    assert records["m100"]["archive_status"] == "failed"
    assert records["m100"]["archive_next_attempt_at"]


def test_remote_mid_search_uses_exact_filter(tmp_path):
    captured = []

    def search(request):
        captured.append(request)
        return SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(items=[SimpleNamespace(fields={"mid": "m1"})]),
        )

    client = SimpleNamespace(
        bitable=SimpleNamespace(
            v1=SimpleNamespace(app_table_record=SimpleNamespace(search=search))
        )
    )
    syncer = BitableSyncer(Settings(), client, ForwardStore(tmp_path / "f.json"))
    syncer._app_token = "app"
    syncer._table_id = "table"

    assert syncer._remote_has_mid("m1")
    request = captured[0]
    condition = request.request_body.filter.conditions[0]
    assert request.page_size == 1
    assert condition.field_name == "mid"
    assert condition.operator == "is"
    assert condition.value == ["m1"]


def test_missing_target_table_never_falls_back_to_unrelated_table(tmp_path, monkeypatch):
    syncer = BitableSyncer(
        Settings(bitable_url="https://x.feishu.cn/base/App123"),
        None,
        ForwardStore(tmp_path / "f.json"),
    )
    monkeypatch.setattr(syncer, "_resolve_app_token", lambda: "App123")
    monkeypatch.setattr(syncer, "_list_tables", lambda: [("tbl_unrelated", "财务数据")])
    monkeypatch.setattr(
        syncer,
        "_create_table",
        lambda name: (_ for _ in ()).throw(BitableError("permission denied")),
    )

    with pytest.raises(BitableError, match="permission denied"):
        syncer._ensure_table()
    assert syncer._table_id == ""
