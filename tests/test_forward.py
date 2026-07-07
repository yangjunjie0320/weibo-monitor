from src.card_store import CardStore
from src.config import Settings
from src.forward import ForwardService, ForwardStore
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


def test_card_store_fifo(tmp_path):
    store = CardStore(tmp_path / "cards.json", max_entries=2)
    store.put("om_1", make_entry(mid="m1"))
    store.put("om_2", make_entry(mid="m2"))
    store.put("om_3", make_entry(mid="m3"))
    assert store.get("om_1") is None
    assert store.get("om_3")["mid"] == "m3"
    # 重开加载持久化内容
    reloaded = CardStore(tmp_path / "cards.json", max_entries=2)
    assert reloaded.get("om_2")["mid"] == "m2"


def test_forward_store_dedup_and_sync(tmp_path):
    store = ForwardStore(tmp_path / "forwarded.json")
    assert store.add("m1", {"synced": False})
    assert not store.add("m1", {"synced": False})
    assert [mid for mid, _ in store.pending()] == ["m1"]
    store.mark_synced(["m1"])
    assert store.pending() == []
    # 重开加载持久化内容
    reloaded = ForwardStore(tmp_path / "forwarded.json")
    assert reloaded.pending() == []
    assert not reloaded.add("m1", {"synced": False})


def test_accept_validates_and_records(tmp_path):
    card_store = CardStore(tmp_path / "cards.json")
    forward_store = ForwardStore(tmp_path / "forwarded.json")
    service = ForwardService(Settings(), None, card_store, forward_store)

    ok, toast = service.accept(make_event(message_id="om_unknown"))
    assert not ok and "无法转发" in toast

    card_store.put("om_1", make_entry())
    ok, toast = service.accept(make_event())
    assert ok and "已转发" in toast
    record = dict(forward_store.pending())["m1"]
    assert record["screen_name"] == "测试博主"
    assert record["summary"] == "今天试驾了一台新车"
    assert record["forwarder_open_id"] == "ou_laoma"
    assert not record["synced"]

    ok, toast = service.accept(make_event())
    assert not ok and "已转发过" in toast
