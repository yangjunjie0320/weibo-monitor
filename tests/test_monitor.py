import datetime as dt
from unittest.mock import AsyncMock

from src.config import Settings
from src.health import HealthStore
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
        cookie_refresh_enabled=False,
        forward_enabled=False,
    )
    state = state or StateStore(settings.state_file)
    pusher = pusher or FakePusher()
    health = HealthStore(tmp_path / "health.json")
    monitor = Monitor(settings, FakeWeibo(pages), state, pusher, [ACCOUNT], health)
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


async def test_official_source_prepares_once_and_skips_account_delays(tmp_path, monkeypatch):
    class PreparedWeibo(FakeWeibo):
        def __init__(self):
            super().__init__({1: timeline(make_card("m1", dt.timedelta(hours=1)))})
            self.prepared = []

        async def prepare_cycle(self, accounts):
            self.prepared.append([account.uid for account in accounts])

    configured = Settings(
        weibo_source="official_cli",
        state_file=str(tmp_path / "seen.json"),
        account_delay_min_seconds=8,
        account_delay_max_seconds=15,
        cookie_refresh_enabled=False,
        forward_enabled=False,
    )
    client = PreparedWeibo()
    sleeper = AsyncMock()
    monkeypatch.setattr("src.monitor.asyncio.sleep", sleeper)
    monitor = Monitor(
        configured,
        client,
        StateStore(configured.state_file),
        FakePusher(),
        [ACCOUNT, Account(name="第二个", uid="43")],
        HealthStore(tmp_path / "health.json"),
    )

    await monitor.run_cycle()

    assert client.prepared == [["42", "43"]]
    sleeper.assert_not_awaited()


async def test_account_delay_follows_client_active_source(tmp_path, monkeypatch):
    """hybrid 轮中切到 CLI 后（requires_account_delay=False）不再做防封延迟。"""

    class NoDelayWeibo(FakeWeibo):
        requires_account_delay = False

    configured = Settings(
        weibo_source="hybrid",
        state_file=str(tmp_path / "seen.json"),
        account_delay_min_seconds=8,
        account_delay_max_seconds=15,
        cookie_refresh_enabled=False,
        legacy_extend_enabled=False,
        forward_enabled=False,
    )
    sleeper = AsyncMock()
    monkeypatch.setattr("src.monitor.asyncio.sleep", sleeper)
    monitor = Monitor(
        configured,
        NoDelayWeibo({1: timeline()}),
        StateStore(configured.state_file),
        FakePusher(),
        [ACCOUNT, Account(name="第二个", uid="43")],
        HealthStore(tmp_path / "health.json"),
    )

    summary = await monitor.run_cycle()

    sleeper.assert_not_awaited()
    assert summary["source"] == "hybrid"


async def test_official_source_refreshes_cookie_for_legacy_extend(tmp_path, monkeypatch):
    """official_cli 模式下长文展开仍依赖 cookie，过期时也应刷新并重载。"""

    class ReloadingWeibo(FakeWeibo):
        def __init__(self):
            super().__init__({1: timeline()})
            self.reloads = 0

        def reload_static_cookie(self):
            self.reloads += 1

    configured = Settings(
        weibo_source="official_cli",
        legacy_extend_enabled=True,
        state_file=str(tmp_path / "seen.json"),
        account_delay_min_seconds=0,
        account_delay_max_seconds=0,
        forward_enabled=False,
    )
    refresher = AsyncMock(return_value=True)
    monkeypatch.setattr("src.monitor.ensure_fresh_cookie", refresher)
    client = ReloadingWeibo()
    monitor = Monitor(
        configured,
        client,
        StateStore(configured.state_file),
        FakePusher(),
        [ACCOUNT],
        HealthStore(tmp_path / "health.json"),
    )

    await monitor.run_cycle()

    refresher.assert_awaited_once()
    assert client.reloads == 1


async def test_official_source_skips_cookie_refresh_without_legacy_extend(
    tmp_path, monkeypatch
):
    configured = Settings(
        weibo_source="official_cli",
        legacy_extend_enabled=False,
        state_file=str(tmp_path / "seen.json"),
        account_delay_min_seconds=0,
        account_delay_max_seconds=0,
        forward_enabled=False,
    )
    refresher = AsyncMock(return_value=True)
    monkeypatch.setattr("src.monitor.ensure_fresh_cookie", refresher)
    monitor = Monitor(
        configured,
        FakeWeibo({1: timeline()}),
        StateStore(configured.state_file),
        FakePusher(),
        [ACCOUNT],
        HealthStore(tmp_path / "health.json"),
    )

    await monitor.run_cycle()

    refresher.assert_not_awaited()


async def test_official_prepare_rate_limit_aborts_before_accounts(tmp_path):
    from src.weibo import RateLimitedError

    class LimitedPrepare(FakeWeibo):
        async def prepare_cycle(self, accounts):
            raise RateLimitedError("official CLI quota", status_code=429)

        async def timeline_page(self, uid, page):
            raise AssertionError("account polling must not start")

    configured = Settings(
        weibo_source="official_cli",
        state_file=str(tmp_path / "seen.json"),
        cookie_refresh_enabled=False,
        forward_enabled=False,
    )
    monitor = Monitor(
        configured,
        LimitedPrepare({}),
        StateStore(configured.state_file),
        FakePusher(),
        [ACCOUNT],
        HealthStore(tmp_path / "health.json"),
    )

    summary = await monitor.run_cycle()

    assert summary["rate_limited"] is True
    assert summary["attempted"] == 0
    assert summary["failed"] == 1


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


async def test_rate_limited_aborts_cycle(tmp_path):
    from src.weibo import RateLimitedError

    class LimitedWeibo(FakeWeibo):
        async def timeline_page(self, uid: str, page: int) -> dict:
            raise RateLimitedError("captcha challenge")

    settings = Settings(
        account_delay_min_seconds=0,
        account_delay_max_seconds=0,
        state_file=str(tmp_path / "seen.json"),
        cookie_refresh_enabled=False,
        forward_enabled=False,
    )
    state = StateStore(settings.state_file)
    pusher = FakePusher()
    accounts = [ACCOUNT, Account(name="第二个", uid="43")]
    monitor = Monitor(
        settings,
        LimitedWeibo({}),
        state,
        pusher,
        accounts,
        HealthStore(tmp_path / "health.json"),
    )

    summary = await monitor.run_cycle()
    # 熔断：第一个账号触发限流后本轮中止，不再打第二个账号
    assert summary["rate_limited"] == 1
    assert summary["failed"] == 1


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


async def test_rate_limit_on_second_page_is_not_swallowed(tmp_path):
    from src.weibo import RateLimitedError

    class PageTwoLimited(FakeWeibo):
        async def timeline_page(self, uid: str, page: int) -> dict:
            if page == 2:
                raise RateLimitedError("HTTP 432", status_code=432)
            return self.pages.get(page, timeline())

    settings = Settings(
        account_delay_min_seconds=0,
        account_delay_max_seconds=0,
        state_file=str(tmp_path / "seen.json"),
        cookie_refresh_enabled=False,
        forward_enabled=False,
    )
    state = StateStore(settings.state_file)
    state.mark_seen("42", ["known"])
    pusher = FakePusher()
    client = PageTwoLimited({1: timeline(make_card("new", dt.timedelta(minutes=5)))})
    monitor = Monitor(
        settings,
        client,
        state,
        pusher,
        [ACCOUNT],
        HealthStore(tmp_path / "health.json"),
    )

    summary = await monitor.run_cycle()

    assert summary["rate_limited"] is True
    assert summary["attempted"] == 1
    assert pusher.pushed == []


async def test_three_consecutive_upstream_failures_abort_cycle(tmp_path):
    from src.weibo import UpstreamError

    class BrokenWeibo(FakeWeibo):
        async def timeline_page(self, uid: str, page: int) -> dict:
            raise UpstreamError("empty response")

    settings = Settings(
        account_delay_min_seconds=0,
        account_delay_max_seconds=0,
        state_file=str(tmp_path / "seen.json"),
        cookie_refresh_enabled=False,
        upstream_failure_threshold=3,
        forward_enabled=False,
    )
    accounts = [Account(name=f"账号{i}", uid=str(i)) for i in range(5)]
    monitor = Monitor(
        settings,
        BrokenWeibo({}),
        StateStore(settings.state_file),
        FakePusher(),
        accounts,
        HealthStore(tmp_path / "health.json"),
    )

    summary = await monitor.run_cycle()

    assert summary["upstream_aborted"] is True
    assert summary["attempted"] == 3
    assert summary["failed"] == 3
