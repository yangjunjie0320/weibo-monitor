from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import time
from typing import Any, Protocol

from .config import Settings
from .cookie_refresh import ensure_fresh_cookie
from .models import Account, Post
from .state import StateStore
from .weibo import RateLimitedError, WeiboClient, WeiboError, extract_mblogs, parse_post

logger = logging.getLogger(__name__)


class Pusher(Protocol):
    async def push(self, post: Post) -> bool: ...


class Monitor:
    def __init__(
        self,
        settings: Settings,
        client: WeiboClient,
        state: StateStore,
        pusher: Pusher,
        accounts: list[Account],
    ) -> None:
        self._settings = settings
        self._client = client
        self._state = state
        self._pusher = pusher
        self._accounts = accounts

    async def run_forever(self) -> None:
        rate_limited_streak = 0
        while True:
            started = time.monotonic()
            summary: dict[str, int] = {}
            try:
                summary = await self.run_cycle()
            except Exception:
                logger.exception("cycle failed unexpectedly")
            elapsed = time.monotonic() - started
            delay = max(self._settings.poll_interval_seconds - elapsed, 30.0)
            if summary.get("rate_limited"):
                rate_limited_streak += 1
                rest = min(
                    self._settings.rate_limit_rest_seconds * 2 ** (rate_limited_streak - 1),
                    self._settings.rate_limit_rest_max_seconds,
                )
                delay = max(delay, rest)
                logger.warning(
                    "rate limited %d cycle(s) in a row, resting %.0fs before next cycle",
                    rate_limited_streak,
                    delay,
                )
            else:
                rate_limited_streak = 0
            logger.info("cycle done in %.0fs, next in %.0fs", elapsed, delay)
            await asyncio.sleep(delay)

    async def run_cycle(self) -> dict[str, int]:
        if await ensure_fresh_cookie(self._settings):
            self._client.reload_static_cookie()
        accounts = list(self._accounts)
        random.shuffle(accounts)
        summary = {"accounts": len(accounts), "new": 0, "pushed": 0, "failed": 0}

        for index, account in enumerate(accounts):
            try:
                new, pushed = await self._poll_account(account)
                summary["new"] += new
                summary["pushed"] += pushed
            except RateLimitedError as exc:
                summary["failed"] += 1
                summary["rate_limited"] = 1
                logger.warning(
                    "rate limited at account %s (uid=%s), aborting cycle: %s",
                    account.name,
                    account.uid,
                    exc,
                )
                self._state.save()
                break
            except Exception as exc:
                summary["failed"] += 1
                logger.warning(
                    "account poll failed: name=%s uid=%s error=%s",
                    account.name,
                    account.uid,
                    exc,
                )
            self._state.save()
            if index < len(accounts) - 1:
                await asyncio.sleep(
                    random.uniform(
                        self._settings.account_delay_min_seconds,
                        self._settings.account_delay_max_seconds,
                    )
                )

        logger.info(
            "cycle summary: accounts=%d new=%d pushed=%d failed=%d",
            summary["accounts"],
            summary["new"],
            summary["pushed"],
            summary["failed"],
        )
        return summary

    async def _poll_account(self, account: Account) -> tuple[int, int]:
        uid = account.uid
        now_iso = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        seeded = self._state.has_account(uid)

        entries = await self._fetch_entries(account, max_pages=1 if not seeded else None)
        if not entries:
            # 页面为空不能当作正常状态落库，避免误把后续帖子全判为新
            if seeded:
                self._state.mark_seen(uid, [], last_poll=now_iso)
            logger.info("no posts parsed: name=%s uid=%s", account.name, uid)
            return 0, 0

        if not seeded:
            # 首次见到该账号：全部落 state，不推送，防冷启动刷屏
            self._state.mark_seen(uid, [post.mid for _, post in entries], last_poll=now_iso)
            logger.info(
                "account seeded: name=%s uid=%s mids=%d", account.name, uid, len(entries)
            )
            return 0, 0

        max_age = dt.timedelta(hours=self._settings.max_post_age_hours)
        now = dt.datetime.now(dt.UTC)
        new_posts: list[tuple[dict[str, Any], Post]] = []
        stale_mids: list[str] = []
        for mblog, post in entries:
            if self._state.is_seen(uid, post.mid):
                continue
            if now - post.created_at > max_age:
                # 超龄的未见帖（如翻出的旧置顶）：静默落 state
                stale_mids.append(post.mid)
                continue
            new_posts.append((mblog, post))

        if stale_mids:
            self._state.mark_seen(uid, stale_mids)

        pushed = 0
        for mblog, post in sorted(new_posts, key=lambda item: item[1].created_at):
            if mblog.get("isLongText"):
                extend = await self._client.fetch_extend(post.mid)
                refreshed = parse_post(account, {}, mblog, extend)
                if refreshed:
                    refreshed.is_pinned = post.is_pinned
                    post = refreshed
            if await self._pusher.push(post):
                pushed += 1
                self._state.mark_seen(uid, [post.mid])
            else:
                logger.error(
                    "push failed, will retry next cycle: uid=%s mid=%s", uid, post.mid
                )

        self._state.mark_seen(uid, [], last_poll=now_iso)
        if new_posts:
            logger.info(
                "account polled: name=%s uid=%s new=%d pushed=%d",
                account.name,
                uid,
                len(new_posts),
                pushed,
            )
        return len(new_posts), pushed

    async def _fetch_entries(
        self, account: Account, max_pages: int | None = None
    ) -> list[tuple[dict[str, Any], Post]]:
        """抓取时间线并解析。翻页直到遇到已见/超龄帖子或到达页数上限。"""
        uid = account.uid
        limit = max_pages or self._settings.max_pages_per_account
        max_age = dt.timedelta(hours=self._settings.max_post_age_hours)
        now = dt.datetime.now(dt.UTC)
        entries: list[tuple[dict[str, Any], Post]] = []
        seen_mids: set[str] = set()

        for page in range(1, limit + 1):
            try:
                data = await self._client.timeline_page(uid, page)
            except WeiboError:
                if entries:
                    break  # 已有部分数据，按部分结果处理
                raise
            pairs = extract_mblogs(data)
            if not pairs:
                break

            reached_known = False
            for card, mblog in pairs:
                post = parse_post(account, card, mblog)
                if not post or post.mid in seen_mids:
                    continue
                seen_mids.add(post.mid)
                entries.append((mblog, post))
                if not post.is_pinned and (
                    self._state.is_seen(uid, post.mid) or now - post.created_at > max_age
                ):
                    reached_known = True

            if reached_known or page >= limit:
                break
            await asyncio.sleep(
                random.uniform(
                    self._settings.account_delay_min_seconds,
                    self._settings.account_delay_max_seconds,
                )
            )

        return entries
