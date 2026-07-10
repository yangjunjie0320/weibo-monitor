import datetime as dt
import json
from types import SimpleNamespace

import pytest

import src.forward as forward_module
from src.card_store import CardStore
from src.config import Settings
from src.forward import RETRY_DELAYS_SECONDS, ForwardService, ForwardStore
from src.listener import ForwardEvent


def make_event(**overrides) -> ForwardEvent:
    base = {
        "mid": "m1",
        "uid": "42",
        "message_id": "om_1",
        "chat_id": "oc_1",
        "operator_open_id": "ou_laoma",
    }
    base.update(overrides)
    return ForwardEvent(**base)


def make_entry(**overrides) -> dict:
    base = {
        "card": {"schema": "2.0", "body": {"elements": []}},
        "mid": "m1",
        "uid": "42",
        "screen_name": "测试博主",
        "label": "市场数据",
        "summary": "今天试驾了一台新车",
        "url": "https://weibo.com/42/Babc",
        "post_created_at": "2026-07-01T12:30:00+08:00",
    }
    base.update(overrides)
    return base


def make_service(tmp_path, client=None):
    card_store = CardStore(tmp_path / "cards.json")
    forward_store = ForwardStore(tmp_path / "forwarded.json")
    service = ForwardService(
        Settings(forward_chat_id="oc_target"),
        client,
        card_store,
        forward_store,
        retry_poll_seconds=0.01,
    )
    return service, card_store, forward_store


def test_card_store_fifo(tmp_path):
    store = CardStore(tmp_path / "cards.json", max_entries=2)
    store.put("om_1", make_entry(mid="m1"))
    store.put("om_2", make_entry(mid="m2"))
    store.put("om_3", make_entry(mid="m3"))
    assert store.get("om_1") is None
    assert store.get("om_3")["mid"] == "m3"
    reloaded = CardStore(tmp_path / "cards.json", max_entries=2)
    assert reloaded.get("om_2")["mid"] == "m2"


def test_forward_store_migrates_v1_without_redelivery(tmp_path):
    path = tmp_path / "forwarded.json"
    path.write_text(
        json.dumps(
            {
                "forwards": {
                    "archived": {"synced": True, "screen_name": "A"},
                    "pending": {"synced": False, "screen_name": "B"},
                }
            }
        ),
        encoding="utf-8",
    )

    store = ForwardStore(path)
    archived = store.get("archived")
    pending = store.get("pending")
    assert archived["delivery_status"] == "forwarded"
    assert archived["archive_status"] == "synced"
    assert pending["delivery_status"] == "forwarded"
    assert pending["archive_status"] == "pending"
    assert store.due_delivery() == []
    assert [mid for mid, _ in store.pending_archive()] == ["pending"]
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 2
    assert "synced" not in persisted["forwards"]["pending"]


def test_delivery_retry_schedule_and_manual_requeue(tmp_path):
    store = ForwardStore(tmp_path / "forwarded.json")
    start = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    assert store.enqueue("m1", {"message_id": "om_1"}, now=start) == "created"

    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        now = start + dt.timedelta(days=attempt)
        store.mark_delivery_failed("m1", "temporary", now=now)
        rec = store.get("m1")
        assert rec["delivery_status"] == "failed"
        assert rec["attempts"] == attempt
        assert dt.datetime.fromisoformat(rec["next_attempt_at"]) == now + dt.timedelta(
            seconds=delay
        )
        assert [
            mid
            for mid, _ in store.due_delivery(
                now=now + dt.timedelta(seconds=delay + 1)
            )
        ] == ["m1"]
        assert store.enqueue("m1", {"message_id": "must-not-reset"}, now=now) == "queued"
        assert store.get("m1")["attempts"] == attempt

    store.mark_delivery_failed("m1", "exhausted", now=start + dt.timedelta(days=10))
    rec = store.get("m1")
    assert rec["delivery_status"] == "failed"
    assert rec["next_attempt_at"] is None

    assert store.enqueue("m1", {"message_id": "om_2"}, now=start) == "requeued"
    rec = store.get("m1")
    assert rec["delivery_status"] == "queued"
    assert rec["attempts"] == 0
    assert rec["message_id"] == "om_2"


def test_accept_validates_mid_and_queues(tmp_path):
    service, card_store, forward_store = make_service(tmp_path)

    ok, toast = service.accept(make_event(message_id="om_unknown"))
    assert not ok and "无法转发" in toast

    card_store.put("om_1", make_entry())
    ok, toast = service.accept(make_event(mid="forged"))
    assert not ok and "不匹配" in toast
    assert forward_store.records() == {}

    ok, toast = service.accept(make_event())
    assert ok and toast == "已加入转发队列"
    record = forward_store.get("m1")
    assert record["screen_name"] == "测试博主"
    assert record["forwarder_open_id"] == "ou_laoma"
    assert record["delivery_status"] == "queued"
    assert record["card_status"] == "pending"
    assert record["archive_status"] == "pending"

    ok, toast = service.accept(make_event())
    assert not ok and "队列" in toast


class _Messages:
    def __init__(self, *, forward_ok=True, patch_ok=True):
        self.forward_ok = forward_ok
        self.patch_ok = patch_ok
        self.calls = []

    def forward(self, request):
        self.calls.append("forward")
        return SimpleNamespace(
            success=lambda: self.forward_ok,
            code=0 if self.forward_ok else 500,
            msg="ok" if self.forward_ok else "failed",
        )

    def patch(self, request):
        self.calls.append("patch")
        return SimpleNamespace(
            success=lambda: self.patch_ok,
            code=0 if self.patch_ok else 500,
            msg="ok" if self.patch_ok else "failed",
        )


def make_client(messages):
    return SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=messages)))


@pytest.mark.asyncio
async def test_forward_success_is_persisted_before_patch_and_archive(tmp_path):
    messages = _Messages()
    service, card_store, store = make_service(tmp_path, make_client(messages))
    card_store.put("om_1", make_entry())
    event = make_event()
    assert service.accept(event)[0]

    await service.process(event)

    assert messages.calls == ["forward", "patch"]
    rec = store.get("m1")
    assert rec["delivery_status"] == "forwarded"
    assert rec["card_status"] == "patched"
    assert rec["archive_status"] == "pending"
    assert rec["forwarded_at"]
    assert store.due_delivery() == []


@pytest.mark.asyncio
async def test_forward_failure_does_not_patch_or_archive(tmp_path):
    messages = _Messages(forward_ok=False)
    service, card_store, store = make_service(tmp_path, make_client(messages))
    card_store.put("om_1", make_entry())
    event = make_event()
    assert service.accept(event)[0]

    await service.process(event)

    assert messages.calls == ["forward"]
    rec = store.get("m1")
    assert rec["delivery_status"] == "failed"
    assert rec["card_status"] == "pending"
    assert rec["archive_status"] == "pending"
    assert rec["attempts"] == 1
    assert rec["forwarded_at"] == ""
    assert store.pending_archive() == []


@pytest.mark.asyncio
async def test_patch_failure_retries_without_forwarding_again(tmp_path, monkeypatch):
    messages = _Messages(patch_ok=False)
    service, card_store, store = make_service(tmp_path, make_client(messages))
    card_store.put("om_1", make_entry())
    event = make_event()
    assert service.accept(event)[0]
    await service.process(event)
    assert messages.calls == ["forward", "patch"]
    assert store.get("m1")["card_status"] == "failed"
    assert store.pending_archive() == []

    messages.patch_ok = True
    future = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=2)
    due = store.due_cards(now=future)
    assert [mid for mid, _ in due] == ["m1"]
    monkeypatch.setattr(forward_module, "_utcnow", lambda: future)
    await service._process_card("m1")

    assert messages.calls == ["forward", "patch", "patch"]
    assert store.get("m1")["card_status"] == "patched"
    assert [mid for mid, _ in store.pending_archive()] == ["m1"]


@pytest.mark.asyncio
async def test_process_due_recovers_persisted_queue(tmp_path):
    messages = _Messages()
    service, card_store, store = make_service(tmp_path, make_client(messages))
    card_store.put("om_1", make_entry())
    assert service.accept(make_event())[0]

    # A new service instance represents process restart; the persisted card is enough.
    recovered = ForwardService(
        Settings(forward_chat_id="oc_target"),
        make_client(messages),
        card_store,
        ForwardStore(tmp_path / "forwarded.json"),
    )
    await recovered.process_due()

    assert messages.calls == ["forward", "patch"]
    assert store.get("m1")["delivery_status"] == "queued"  # old in-memory snapshot
    assert ForwardStore(tmp_path / "forwarded.json").get("m1")["delivery_status"] == "forwarded"
