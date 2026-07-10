from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import html
import json
import logging
import random
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .models import Account, Post, VideoInfo

logger = logging.getLogger(__name__)

# UA 池：每个游客会话固定一个 UA（请求间换 UA 反而像机器人）
USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
]

MOBILE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://m.weibo.cn/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


class WeiboError(Exception):
    pass


class AuthenticationError(WeiboError):
    """微博登录态失效；调用方最多刷新一次 Cookie。"""


class UpstreamError(WeiboError):
    """微博上游临时故障，可做有限次数重试。"""


class SourceConfigurationError(WeiboError):
    """数据源安装、命令或套餐配置不满足运行要求。"""


class RateLimitedError(WeiboError):
    """captcha/visitor 挑战：IP 级限流，重试无益，调用方应熔断休息。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def _load_static_cookie(settings: Settings) -> str:
    if settings.weibo_cookie_file:
        path = Path(settings.weibo_cookie_file)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        logger.warning("weibo_cookie_file not found: %s", path)
    return settings.weibo_cookie.strip()


class WeiboClient:
    """m.weibo.cn container API 客户端。

    配置了真实 cookie（weibo_cookie / weibo_cookie_file）时优先使用；
    否则用游客 cookie（启动时获取，遇挑战自动换新）。真实 cookie 遇到
    挑战时直接抛 RateLimitedError（换 cookie 无益，只能降速休息）。
    """

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http_client
        self._static = _load_static_cookie(settings)
        self._cookie = self._static
        self._ua = random.choice(USER_AGENTS)

    def _headers(self) -> dict[str, str]:
        headers = dict(MOBILE_HEADERS)
        headers["User-Agent"] = self._ua
        return headers

    @property
    def uses_static_cookie(self) -> bool:
        return bool(self._static)

    def reload_static_cookie(self) -> None:
        """cookie 文件被自动刷新后重新加载。"""
        refreshed = _load_static_cookie(self._settings)
        if refreshed and refreshed != self._static:
            self._static = refreshed
            self._cookie = refreshed
            logger.info("static cookie reloaded")

    async def ensure_cookie(self) -> None:
        if self._static:
            logger.info("using configured weibo cookie")
            return
        await self.refresh_visitor_cookie()

    async def request_json(self, url: str) -> dict[str, Any]:
        last_error: Exception | None = None
        auth_refreshed = False
        upstream_attempt = 0
        while True:
            try:
                return await self._request_json_once(url)
            except RateLimitedError:
                raise  # IP/会话级封控，重试与换游客身份都会加重封控
            except AuthenticationError:
                if auth_refreshed or not await self._refresh_auth_cookie():
                    raise
                auth_refreshed = True
            except (httpx.HTTPError, UpstreamError) as exc:
                last_error = exc
                if upstream_attempt >= self._settings.request_retries:
                    path = urllib.parse.urlsplit(url).path
                    raise UpstreamError(
                        f"request failed: endpoint={path} error={last_error}"
                    ) from last_error
                upstream_attempt += 1
                await asyncio.sleep(0.8 * upstream_attempt)

    async def _refresh_auth_cookie(self) -> bool:
        if not self._static:
            try:
                await self.refresh_visitor_cookie()
                return True
            except Exception as exc:
                logger.warning("visitor auth refresh failed: %s", exc)
                return False

        from .cookie_refresh import refresh_weibo_cookie

        if not await refresh_weibo_cookie(self._settings):
            return False
        self.reload_static_cookie()
        return bool(self._static)

    async def _request_json_once(self, url: str) -> dict[str, Any]:
        headers = self._headers()
        if self._cookie:
            headers["Cookie"] = self._cookie
        try:
            resp = await self._http.get(
                url, headers=headers, timeout=self._settings.request_timeout
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"transport error: {type(exc).__name__}") from exc

        status = resp.status_code
        content_type = resp.headers.get("content-type", "")
        size = len(resp.content)
        if status == 401:
            raise AuthenticationError("HTTP 401 authentication rejected")
        if status in {403, 418, 429, 432}:
            retry_after = _parse_retry_after(resp.headers.get("retry-after"))
            logger.warning(
                "weibo rate limited: status=%d content_type=%s bytes=%d retry_after=%s",
                status,
                content_type or "-",
                size,
                retry_after,
            )
            raise RateLimitedError(
                f"HTTP {status}",
                status_code=status,
                retry_after_seconds=retry_after,
            )
        if status >= 500:
            raise UpstreamError(
                f"upstream HTTP {status}: content_type={content_type or '-'} bytes={size}"
            )
        if status >= 400:
            raise WeiboError(
                f"request rejected: HTTP {status} content_type={content_type or '-'} bytes={size}"
            )
        if not resp.content:
            raise UpstreamError(
                f"empty response: HTTP {status} content_type={content_type or '-'}"
            )

        text = resp.text
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            if "Sina Visitor System" in text:
                raise RateLimitedError("visitor challenge", status_code=status) from None
            raise UpstreamError(
                f"invalid JSON: HTTP {status} content_type={content_type or '-'} bytes={size}"
            ) from exc
        if not isinstance(parsed, dict):
            raise UpstreamError("unexpected JSON shape: expected object")

        ok = parsed.get("ok")
        challenge = " ".join(
            str(parsed.get(key, "")) for key in ("url", "msg", "message", "errmsg")
        ).lower()
        if ok == -100 and ("captcha" in challenge or "visitor" in challenge):
            raise RateLimitedError("captcha challenge", status_code=status)
        if ok != 1:
            if "captcha" in challenge or "visitor" in challenge:
                raise RateLimitedError("challenge response", status_code=status)
            if "login" in challenge or "登录" in challenge:
                raise AuthenticationError("API reports authentication required")
            raise UpstreamError(f"unexpected API status: ok={ok!r}")
        return parsed

    async def refresh_visitor_cookie(self) -> None:
        self._ua = random.choice(USER_AGENTS)  # 新游客身份配新 UA
        payload = {
            "cb": "visitor_gray_callback",
            "ver": "20250916",
            "request_id": f"weibo_monitor_{int(time.time() * 1000)}",
            "tid": "",
            "from": "weibo",
            "webdriver": "false",
            "rid": str(int(time.time() * 1000)),
            "return_url": "https://m.weibo.cn/",
        }
        resp = await self._http.post(
            "https://visitor.passport.weibo.cn/visitor/genvisitor2",
            data=payload,
            headers=self._headers(),
            timeout=self._settings.request_timeout,
        )
        status = resp.status_code
        content_type = resp.headers.get("content-type", "")
        size = len(resp.content)
        if status in {403, 418, 429, 432}:
            raise RateLimitedError(
                f"visitor endpoint HTTP {status}",
                status_code=status,
                retry_after_seconds=_parse_retry_after(resp.headers.get("retry-after")),
            )
        if status >= 500:
            raise UpstreamError(
                f"visitor endpoint HTTP {status}: "
                f"content_type={content_type or '-'} bytes={size}"
            )
        if status >= 400:
            raise WeiboError(
                f"visitor endpoint rejected: HTTP {status} "
                f"content_type={content_type or '-'} bytes={size}"
            )
        match = re.search(r"visitor_gray_callback\((\{.*\})\);?", resp.text)
        if not match:
            raise UpstreamError(
                f"invalid visitor response: HTTP {status} "
                f"content_type={content_type or '-'} bytes={size}"
            )
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise UpstreamError(
                f"invalid visitor JSON: HTTP {status} "
                f"content_type={content_type or '-'} bytes={size}"
            ) from exc
        if not isinstance(data, dict):
            raise UpstreamError("unexpected visitor JSON shape: expected object")
        if data.get("retcode") != 20000000:
            raise UpstreamError(
                f"visitor cookie rejected: HTTP {status} retcode={data.get('retcode')!r}"
            )
        try:
            sub = data["data"]["sub"]
            subp = data["data"]["subp"]
        except (KeyError, TypeError) as exc:
            raise UpstreamError("visitor response missing cookie fields") from exc
        self._cookie = f"SUB={sub}; SUBP={subp}"
        logger.info("visitor cookie refreshed")
        await self._warmup()

    async def _warmup(self) -> None:
        try:
            headers = self._headers()
            headers["Cookie"] = self._cookie
            await self._http.get("https://m.weibo.cn/", headers=headers, timeout=10)
        except httpx.HTTPError:
            pass

    async def timeline_page(self, uid: str, page: int) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "type": "uid",
            "value": uid,
            "containerid": f"107603{uid}",
        }
        if page > 1:
            params["page"] = page
        query = urllib.parse.urlencode(params)
        return await self.request_json(f"https://m.weibo.cn/api/container/getIndex?{query}")

    async def fetch_extend(self, mid: str) -> dict[str, Any]:
        """长文全文，失败时返回空 dict（正文退回截断版，不阻塞）。"""
        try:
            data = await self.request_json(
                f"https://m.weibo.cn/statuses/extend?id={urllib.parse.quote(mid)}"
            )
        except RateLimitedError:
            raise
        except WeiboError as exc:
            logger.warning("extend fetch failed mid=%s: %s", mid, exc)
            return {}
        if data.get("ok") == 1 and isinstance(data.get("data"), dict):
            return data["data"]
        return {}


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        target = email.utils.parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=dt.UTC)
        return max((target - dt.datetime.now(dt.UTC)).total_seconds(), 0.0)
    except (TypeError, ValueError, OverflowError):
        return None


# ---------- 纯解析函数（离线可测） ----------


def parse_weibo_datetime(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.strptime(str(value), "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None


def html_to_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(
        r"<img\b[^>]*\balt=[\"']([^\"']*)[\"'][^>]*>",
        lambda m: m.group(1),
        text,
        flags=re.I,
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_pics(mblog: dict[str, Any]) -> list[str]:
    pics: list[Any] = []
    for pic in mblog.get("pics") or []:
        if isinstance(pic, str):
            pics.append(pic)
            continue
        if not isinstance(pic, dict):
            continue
        large = pic.get("large") or {}
        pics.append(large.get("url") or pic.get("url"))
    if not pics:
        for pic in mblog.get("pic_urls") or []:
            if isinstance(pic, dict) and pic.get("thumbnail_pic"):
                pics.append(pic["thumbnail_pic"])
    if not pics:
        for pid in mblog.get("pic_ids") or []:
            if pid:
                pics.append(f"https://wx1.sinaimg.cn/large/{pid}.jpg")
    if not pics:
        for key in ("original_pic", "bmiddle_pic", "thumbnail_pic"):
            if mblog.get(key):
                pics.append(mblog[key])
                break
    return [pic for pic in pics if pic]


def extract_video_info(mblog: dict[str, Any]) -> VideoInfo | None:
    page_info = mblog.get("page_info") or {}
    if page_info.get("type") != "video":
        return None
    media_info = page_info.get("media_info") or {}
    return VideoInfo(
        object_id=str(page_info.get("object_id") or ""),
        title=str(
            page_info.get("page_title") or page_info.get("title") or media_info.get("name") or ""
        ),
        duration=media_info.get("duration"),
        play_count=str(page_info.get("play_count") or ""),
    )


def extract_mblogs(data: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """从 timeline 响应提取 (card, mblog) 对，兼容 card_type 9 与 11（嵌套组）。"""
    statuses = data.get("statuses")
    if isinstance(statuses, list):
        return [({}, item) for item in statuses if isinstance(item, dict)]

    results = []
    for card in data.get("data", {}).get("cards", []) or []:
        if card.get("card_type") == 11:
            for child in card.get("card_group") or []:
                if child.get("card_type") == 9 and isinstance(child.get("mblog"), dict):
                    results.append((child, child["mblog"]))
        if card.get("card_type") == 9 and isinstance(card.get("mblog"), dict):
            results.append((card, card["mblog"]))
    return results


def is_pinned(card: dict[str, Any], mblog: dict[str, Any]) -> bool:
    if card.get("profile_type_id") == "proweibotop_":
        return True
    title = mblog.get("title")
    return isinstance(title, dict) and title.get("text") == "置顶"


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text_fields(mblog: dict[str, Any], extend: dict[str, Any] | None) -> tuple[str, str]:
    if extend and extend.get("longTextContent"):
        text_html = str(extend["longTextContent"])
    else:
        long_text = mblog.get("longText") or {}
        text_html = str(
            (long_text.get("longTextContent") if isinstance(long_text, dict) else "")
            or mblog.get("longTextContent")
            or mblog.get("raw_text")
            or mblog.get("text_raw")
            or mblog.get("text")
            or ""
        )
    return text_html, html_to_text(text_html)


def parse_post(
    account: Account,
    card: dict[str, Any],
    mblog: dict[str, Any],
    extend: dict[str, Any] | None = None,
) -> Post | None:
    created_at = parse_weibo_datetime(mblog.get("created_at"))
    if not created_at:
        return None
    mid = str(mblog.get("mid") or mblog.get("idstr") or mblog.get("id") or "")
    if not mid:
        return None

    text_html, text_plain = _text_fields(mblog, extend)
    retweeted = mblog.get("retweeted_status") or {}
    retweeted_extend = (extend or {}).get("retweeted_status") or {}
    if retweeted:
        _, retweeted_text_plain = _text_fields(retweeted, retweeted_extend)
    else:
        retweeted_text_plain = ""

    user = mblog.get("user") or {}
    has_embedded_long_text = bool(
        mblog.get("longTextContent")
        or (
            isinstance(mblog.get("longText"), dict)
            and mblog["longText"].get("longTextContent")
        )
        or (extend and extend.get("longTextContent"))
    )
    return Post(
        uid=account.uid,
        screen_name=str(user.get("screen_name") or account.name),
        mid=mid,
        bid=str(mblog.get("bid") or mblog.get("mblogid") or ""),
        created_at=created_at,
        is_pinned=is_pinned(card, mblog),
        is_repost=bool(retweeted),
        text_html=text_html,
        text_plain=text_plain,
        source=html_to_text(mblog.get("source")),
        region_name=str(mblog.get("region_name") or ""),
        reposts_count=_int(mblog.get("reposts_count")),
        comments_count=_int(mblog.get("comments_count")),
        attitudes_count=_int(mblog.get("attitudes_count")),
        image_urls=extract_pics(mblog),
        video=extract_video_info(mblog),
        retweeted_screen_name=str((retweeted.get("user") or {}).get("screen_name") or ""),
        retweeted_text_plain=retweeted_text_plain,
        text_truncated=bool(mblog.get("isLongText") or mblog.get("truncated"))
        and not has_embedded_long_text,
    )
