from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import time
from typing import Any, Protocol

from .config import Settings
from .cookie_refresh import ensure_fresh_cookie
from .health import HealthStore, empty_cycle, utc_now
from .models import Account, Post, PushResult
from .state import StateStore
from .weibo import (
    AuthenticationError,
    RateLimitedError,
    SourceConfigurationError,
    UpstreamError,
    WeiboError,
    extract_mblogs,
    parse_post,
)

logger = logging.getLogger(__name__)


class Pusher(Protocol):
    async def push(self, post: Post) -> bool | PushResult: ...


class TimelineClient(Protocol):
    async def timeline_page(self, uid: str, page: int) -> dict[str, Any]: ...

    async def fetch_extend(self, mid: str) -> dict[str, Any]: ...

    def reload_static_cookie(self) -> None: ...


class Monitor:
    def __init__(
        self,
        settings: Settings,
        client: TimelineClient,
        state: StateStore,
        pusher: Pusher,
        accounts: list[Account],
        health: HealthStore,
    ) -> None:
        self._settings = settings
        self._client = client
        self._state = state
        self._pusher = pusher
        self._accounts = accounts
        self._health = health

    async def run_forever(self) -> None:
        self._health.mark_starting(len(self._accounts))
        while True:
            now = utc_now()
            blocked_until = self._health.blocked_until
            if blocked_until and blocked_until > now:
                self._health.write(
                    status="rate_limited",
                    next_cycle_at=blocked_until,
                    blocked_until=blocked_until,
                    rate_limited_streak=self._health.rate_limited_streak,
                )
                await asyncio.sleep((blocked_until - now).total_seconds())

            started = time.monotonic()
            summary: dict[str, Any]
            try:
                summary = await self.run_cycle()
            except Exception as exc:
                logger.exception("cycle failed unexpectedly")
                summary = empty_cycle(len(self._accounts))
                summary["failed"] = 1
                summary["last_error"] = _error("internal", exc)
                summary["internal_failed"] = True
            elapsed = time.monotonic() - started
            delay = max(self._settings.poll_interval_seconds - elapsed, 30.0)

            if summary.get("rate_limited"):
                streak = self._health.rate_limited_streak + 1
                exponential = min(
                    self._settings.rate_limit_rest_seconds * 2 ** (streak - 1),
                    self._settings.rate_limit_rest_max_seconds,
                )
                jitter = self._settings.rate_limit_jitter_ratio
                rest = exponential * random.uniform(1.0 - jitter, 1.0 + jitter)
                retry_after = float(summary.get("retry_after_seconds") or 0)
                delay = max(delay, rest, retry_after)
                blocked_until = utc_now() + dt.timedelta(seconds=delay)
                logger.warning(
                    "rate limited %d cycle(s) in a row, resting %.0fs before next cycle",
                    streak,
                    delay,
                )
                self._health.write(
                    status="rate_limited",
                    cycle=_cycle_stats(summary),
                    next_cycle_at=blocked_until,
                    blocked_until=blocked_until,
                    rate_limited_streak=streak,
                    last_error=summary.get("last_error"),
                )
            elif _is_healthy(summary):
                next_cycle_at = utc_now() + dt.timedelta(seconds=delay)
                self._health.write(
                    status="healthy",
                    cycle=_cycle_stats(summary),
                    next_cycle_at=next_cycle_at,
                    blocked_until=None,
                    rate_limited_streak=0,
                    last_error=None,
                    mark_healthy=True,
                )
            else:
                if summary.get("upstream_aborted") or summary.get("internal_failed"):
                    delay = max(delay, self._settings.upstream_error_rest_seconds)
                next_cycle_at = utc_now() + dt.timedelta(seconds=delay)
                status = "failed" if summary.get("internal_failed") else "degraded"
                self._health.write(
                    status=status,
                    cycle=_cycle_stats(summary),
                    next_cycle_at=next_cycle_at,
                    blocked_until=None,
                    rate_limited_streak=self._health.rate_limited_streak,
                    last_error=summary.get("last_error"),
                )
            logger.info("cycle done in %.0fs, next in %.0fs", elapsed, delay)
            await asyncio.sleep(delay)

    async def run_cycle(self) -> dict[str, Any]:
        # official_cli 模式下长文展开仍走 m.weibo.cn，同样依赖登录 cookie
        needs_cookie = (
            self._settings.weibo_source in ("mobile", "hybrid")
            or self._settings.legacy_extend_enabled
        )
        if needs_cookie and await ensure_fresh_cookie(self._settings):
            self._client.reload_static_cookie()
        accounts = list(self._accounts)
        random.shuffle(accounts)
        summary: dict[str, Any] = empty_cycle(len(accounts))
        summary["source"] = self._settings.weibo_source
        consecutive_upstream_failures = 0
        self._health.write(status="starting", cycle=_cycle_stats(summary), last_error=None)

        prepare_cycle = getattr(self._client, "prepare_cycle", None)
        if prepare_cycle is not None:
            try:
                await prepare_cycle(self._accounts)
                summary["source_requests"] = int(
                    getattr(self._client, "last_cycle_requests", 0)
                )
            except RateLimitedError as exc:
                summary["failed"] = 1
                summary["rate_limited"] = True
                summary["retry_after_seconds"] = exc.retry_after_seconds or 0
                summary["last_error"] = _error("rate_limited", exc)
                return summary
            except (AuthenticationError, SourceConfigurationError) as exc:
                summary["failed"] = 1
                summary["upstream_aborted"] = True
                summary["last_error"] = _error("source_configuration", exc)
                return summary
            except UpstreamError as exc:
                summary["failed"] = 1
                summary["upstream_aborted"] = True
                summary["last_error"] = _error("upstream", exc)
                return summary

        for index, account in enumerate(accounts):
            summary["attempted"] += 1
            try:
                new, pushed, dropped = await self._poll_account(account)
                summary["succeeded"] += 1
                summary["new"] += new
                summary["pushed"] += pushed
                summary["dropped"] += dropped
                consecutive_upstream_failures = 0
            except RateLimitedError as exc:
                summary["failed"] += 1
                summary["rate_limited"] = True
                summary["retry_after_seconds"] = exc.retry_after_seconds or 0
                summary["last_error"] = _error("rate_limited", exc)
                logger.warning(
                    "rate limited at account %s (uid=%s), aborting cycle: %s",
                    account.name,
                    account.uid,
                    exc,
                )
                self._state.save()
                break
            except AuthenticationError as exc:
                summary["failed"] += 1
                summary["upstream_aborted"] = True
                summary["last_error"] = _error("authentication", exc)
                logger.error(
                    "weibo authentication failed at account %s (uid=%s), aborting cycle: %s",
                    account.name,
                    account.uid,
                    exc,
                )
                self._state.save()
                break
            except UpstreamError as exc:
                summary["failed"] += 1
                consecutive_upstream_failures += 1
                summary["last_error"] = _error("upstream", exc)
                logger.warning(
                    "account upstream failure: name=%s uid=%s consecutive=%d error=%s",
                    account.name,
                    account.uid,
                    consecutive_upstream_failures,
                    exc,
                )
                if consecutive_upstream_failures >= self._settings.upstream_failure_threshold:
                    summary["upstream_aborted"] = True
                    self._state.save()
                    break
            except Exception as exc:
                summary["failed"] += 1
                summary["last_error"] = _error("account", exc)
                logger.warning(
                    "account poll failed: name=%s uid=%s error=%s",
                    account.name,
                    account.uid,
                    exc,
                )
            self._state.save()
            self._health.write(
                status="starting",
                cycle=_cycle_stats(summary),
                last_error=summary.get("last_error"),
            )
            # hybrid 轮中可能切源，每次迭代都问 client 当前是否需要防封延迟
            needs_delay = getattr(
                self._client,
                "requires_account_delay",
                self._settings.weibo_source == "mobile",
            )
            if index < len(accounts) - 1 and needs_delay:
                await asyncio.sleep(
                    random.uniform(
                        self._settings.account_delay_min_seconds,
                        self._settings.account_delay_max_seconds,
                    )
                )

        summary["source"] = getattr(
            self._client, "active_source", self._settings.weibo_source
        )
        logger.info(
            "cycle summary: accounts=%d attempted=%d succeeded=%d new=%d pushed=%d "
            "dropped=%d failed=%d rate_limited=%s source_requests=%d source=%s",
            summary["accounts_total"],
            summary["attempted"],
            summary["succeeded"],
            summary["new"],
            summary["pushed"],
            summary["dropped"],
            summary["failed"],
            summary["rate_limited"],
            summary["source_requests"],
            summary["source"],
        )
        return summary

    def finish_once(self, summary: dict[str, Any]) -> None:
        """Persist a terminal status for the `--once` command."""

        if summary.get("rate_limited"):
            delay = max(
                self._settings.rate_limit_rest_seconds,
                float(summary.get("retry_after_seconds") or 0),
            )
            blocked_until = utc_now() + dt.timedelta(seconds=delay)
            self._health.write(
                status="rate_limited",
                cycle=_cycle_stats(summary),
                next_cycle_at=blocked_until,
                blocked_until=blocked_until,
                rate_limited_streak=self._health.rate_limited_streak + 1,
                last_error=summary.get("last_error"),
            )
        elif _is_healthy(summary):
            self._health.write(
                status="healthy",
                cycle=_cycle_stats(summary),
                next_cycle_at=None,
                blocked_until=None,
                rate_limited_streak=0,
                last_error=None,
                mark_healthy=True,
            )
        else:
            self._health.write(
                status="degraded",
                cycle=_cycle_stats(summary),
                next_cycle_at=None,
                blocked_until=None,
                rate_limited_streak=self._health.rate_limited_streak,
                last_error=summary.get("last_error"),
            )

    async def _poll_account(self, account: Account) -> tuple[int, int, int]:
        uid = account.uid
        now_iso = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        seeded = self._state.has_account(uid)

        entries = await self._fetch_entries(account, max_pages=1 if not seeded else None)
        if not entries:
            # 页面为空不能当作正常状态落库，避免误把后续帖子全判为新
            if seeded:
                self._state.mark_seen(uid, [], last_poll=now_iso)
            logger.info("no posts parsed: name=%s uid=%s", account.name, uid)
            return 0, 0, 0

        if not seeded:
            # 首次见到该账号：全部落 state，不推送，防冷启动刷屏
            self._state.mark_seen(uid, [post.mid for _, post in entries], last_poll=now_iso)
            logger.info(
                "account seeded: name=%s uid=%s mids=%d", account.name, uid, len(entries)
            )
            return 0, 0, 0

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
        dropped = 0
        for mblog, post in sorted(new_posts, key=lambda item: item[1].created_at):
            if post.text_truncated:
                extend = await self._client.fetch_extend(post.mid)
                refreshed = parse_post(account, {}, mblog, extend)
                if refreshed:
                    refreshed.is_pinned = post.is_pinned
                    post = refreshed
            raw_result = await self._pusher.push(post)
            result = (
                raw_result
                if isinstance(raw_result, PushResult)
                else PushResult(handled=bool(raw_result), pushed=bool(raw_result))
            )
            if result.handled:
                pushed += int(result.pushed)
                dropped += int(result.dropped)
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
        return len(new_posts), pushed, dropped

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
            except RateLimitedError:
                raise
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


def _error(kind: str, exc: BaseException) -> dict[str, str]:
    message = " ".join(str(exc).split())[:240]
    return {"kind": kind, "message": message or type(exc).__name__}


def _cycle_stats(summary: dict[str, Any]) -> dict[str, int | bool | str]:
    return {
        "accounts_total": int(summary.get("accounts_total", 0)),
        "attempted": int(summary.get("attempted", 0)),
        "succeeded": int(summary.get("succeeded", 0)),
        "failed": int(summary.get("failed", 0)),
        "new": int(summary.get("new", 0)),
        "pushed": int(summary.get("pushed", 0)),
        "dropped": int(summary.get("dropped", 0)),
        "rate_limited": bool(summary.get("rate_limited", False)),
        "source_requests": int(summary.get("source_requests", 0)),
        "source": str(summary.get("source", "")),
    }


def _is_healthy(summary: dict[str, Any]) -> bool:
    return bool(
        summary.get("attempted") == summary.get("accounts_total")
        and summary.get("succeeded") == summary.get("accounts_total")
        and not summary.get("failed")
        and not summary.get("rate_limited")
    )
