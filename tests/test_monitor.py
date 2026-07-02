import datetime as dt

from src.config import Settings
from src.models import Account, Post
from src.monitor import Monitor
from src.state import StateStore

ACCOUNT = Account(name="测试博主", uid="42")
CST = dt.timezone(dt.timedelta(hours=8))


def weibo_time(delta: dt.timedelta) -> str:
    value = dt.datetime.now(CST) - delta
    return value.strftime("%a %b %d %H:%M:%S %z %Y")


def make_card(mid: str, age: dt.timedelta, *, text: str = "内容", pinned: bool = False) -> dict:
    mblog = {
        "mid": mid,
        "bid": f"B{mid}",
        "created_at": weibo_time(age),
        "text": text,
        "user": {"screen_name": "测试博主"},
    }
    card = {"card_type": 9, "mblog": mblog}
    if pinned:
        card["profile_type_id"] = "proweibotop_"
    return card


def timeline(*cards: dict) -> dict:
    return {"data": {"cards": list(cards)}}


class FakeWeibo:
    def __init__(self, pages: dict[int, dict]) -> None:
        self.pages = pages

    async def timeline_page(self, uid: str, page: int) -> dict:
        return self.pages.get(page, timeline())

    async def fetch_extend(self, mid: str) -> dict:
        return {}


class FakePusher:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.pushed: list[Post] = []

    async def push(self, post: Post) -> bool:
        if self.ok:
            self.pushed.append(post)
        return self.ok


def make_monitor(tmp_path, pages, *, state=None, pusher=None):
    settings = Settings(
        account_delay_min_seconds=0,
        account_delay_max_seconds=0,
        state_file=str(tmp_path / "seen.json"),
    )
    state = state or StateStore(settings.state_file)
    pusher = pusher or FakePusher()
    monitor = Monitor(settings, FakeWeibo(pages), state, pusher, [ACCOUNT])
    return monitor, state, pusher


async def test_cold_start_seeds_without_pushing(tmp_path):
    pages = {1: timeline(make_card("m1", dt.timedelta(hours=1)))}
    monitor, state, pusher = make_monitor(tmp_path, pages)

    summary = await monitor.run_cycle()

    assert summary["pushed"] == 0
    assert pusher.pushed == []
    assert state.is_seen("42", "m1")


async def test_new_post_is_pushed_once(tmp_path):
    pages = {
        1: timeline(
            make_card("m2", dt.timedelta(minutes=5), text="新帖"),
            make_card("m1", dt.timedelta(hours=3)),
        )
    }
    monitor, state, pusher = make_monitor(tmp_path, pages)
    state.mark_seen("42", ["m1"])  # 已 seed 过，m1 已见

    summary = await monitor.run_cycle()
    assert summary["pushed"] == 1
    assert [p.mid for p in pusher.pushed] == ["m2"]
    assert state.is_seen("42", "m2")

    # 再跑一轮不重复推送
    summary = await monitor.run_cycle()
    assert summary["pushed"] == 0
    assert len(pusher.pushed) == 1


async def test_stale_unseen_post_marked_silently(tmp_path):
    pages = {1: timeline(make_card("old", dt.timedelta(hours=48)))}
    monitor, state, pusher = make_monitor(tmp_path, pages)
    state.mark_seen("42", ["m0"])

    summary = await monitor.run_cycle()
    assert summary["pushed"] == 0
    assert pusher.pushed == []
    assert state.is_seen("42", "old")


async def test_failed_push_not_marked_seen(tmp_path):
    pages = {
        1: timeline(
            make_card("m2", dt.timedelta(minutes=5)),
            make_card("m1", dt.timedelta(hours=3)),
        )
    }
    pusher = FakePusher(ok=False)
    monitor, state, _ = make_monitor(tmp_path, pages, pusher=pusher)
    state.mark_seen("42", ["m1"])

    await monitor.run_cycle()
    assert not state.is_seen("42", "m2")  # 下轮重试

    pusher.ok = True
    summary = await monitor.run_cycle()
    assert summary["pushed"] == 1
    assert state.is_seen("42", "m2")


async def test_pagination_stops_at_seen_post(tmp_path):
    pages = {
        1: timeline(make_card("m3", dt.timedelta(minutes=5))),
        2: timeline(
            make_card("m2", dt.timedelta(minutes=30)),
            make_card("m1", dt.timedelta(hours=3)),
        ),
        3: timeline(make_card("m0", dt.timedelta(hours=6))),
    }
    monitor, state, pusher = make_monitor(tmp_path, pages)
    state.mark_seen("42", ["m1"])

    summary = await monitor.run_cycle()
    # 第 2 页出现已见的 m1，第 3 页不该再抓，m0 不会被推
    assert sorted(p.mid for p in pusher.pushed) == ["m2", "m3"]
    # 推送按时间正序：先 m2 后 m3
    assert [p.mid for p in pusher.pushed] == ["m2", "m3"]
    assert summary["pushed"] == 2
    assert not state.is_seen("42", "m0")


async def test_new_pinned_post_within_age_is_pushed(tmp_path):
    pages = {
        1: timeline(
            make_card("pin", dt.timedelta(hours=1), pinned=True),
            make_card("m1", dt.timedelta(hours=3)),
        )
    }
    monitor, state, pusher = make_monitor(tmp_path, pages)
    state.mark_seen("42", ["m1"])

    await monitor.run_cycle()
    assert [p.mid for p in pusher.pushed] == ["pin"]
