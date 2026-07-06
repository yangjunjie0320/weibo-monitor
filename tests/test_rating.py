from src.card_store import CardStore
from src.config import Settings
from src.listener import RateEvent
from src.rating import RatingService, RatingStore


def make_event(**overrides) -> RateEvent:
    base = {
        "score": 3,
        "mid": "m1",
        "uid": "42",
        "message_id": "om_1",
        "chat_id": "oc_1",
        "operator_open_id": "ou_laoma",
    }
    base.update(overrides)
    return RateEvent(**base)


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


def test_rating_store_dedup_and_sync(tmp_path):
    store = RatingStore(tmp_path / "ratings.json")
    assert store.add("m1", {"score": 3, "synced": False})
    assert not store.add("m1", {"score": 1, "synced": False})
    assert store.is_rated("m1")
    assert [mid for mid, _ in store.pending()] == ["m1"]
    store.mark_synced(["m1"])
    assert store.pending() == []
    # 重开加载持久化内容
    reloaded = RatingStore(tmp_path / "ratings.json")
    assert reloaded.is_rated("m1")
    assert reloaded.pending() == []


def test_accept_validates_and_records(tmp_path):
    card_store = CardStore(tmp_path / "cards.json")
    rating_store = RatingStore(tmp_path / "ratings.json")
    service = RatingService(Settings(), None, card_store, rating_store)

    ok, toast = service.accept(make_event(message_id="om_unknown"))
    assert not ok and "无法打分" in toast

    card_store.put("om_1", make_entry())
    ok, toast = service.accept(make_event())
    assert ok and "3 分" in toast
    record = dict(rating_store.pending())["m1"]
    assert record["score"] == 3
    assert record["screen_name"] == "测试博主"
    assert record["summary"] == "今天试驾了一台新车"
    assert record["rater_open_id"] == "ou_laoma"
    assert not record["synced"]

    ok, toast = service.accept(make_event(score=1))
    assert not ok and "已打过分" in toast
