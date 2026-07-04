from __future__ import annotations

import asyncio
import datetime as dt
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


class RateLimitedError(WeiboError):
    """captcha/visitor 挑战：IP 级限流，重试无益，调用方应熔断休息。"""


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
        for attempt in range(self._settings.request_retries + 1):
            try:
                return await self._request_json_once(url)
            except RateLimitedError as exc:
                if self._static:
                    raise  # 真实 cookie 被挑战，重试无益
                last_error = exc
                if attempt < self._settings.request_retries:
                    await asyncio.sleep(0.8 * (attempt + 1))
            except (httpx.HTTPError, WeiboError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self._settings.request_retries:
                    await asyncio.sleep(0.8 * (attempt + 1))
        if isinstance(last_error, RateLimitedError):
            raise last_error
        raise WeiboError(f"request failed: {url}: {last_error}") from last_error

    async def _request_json_once(self, url: str) -> dict[str, Any]:
        headers = self._headers()
        if self._cookie:
            headers["Cookie"] = self._cookie
        resp = await self._http.get(url, headers=headers, timeout=self._settings.request_timeout)
        text = resp.text
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            if "Sina Visitor System" in text:
                if not self._static:
                    await self.refresh_visitor_cookie()
                    await asyncio.sleep(1.0)
                raise RateLimitedError("visitor challenge") from None
            raise
        if not isinstance(parsed, dict):
            raise WeiboError(f"unexpected response shape: {text[:200]}")
        if parsed.get("ok") == -100 and "captcha" in str(parsed.get("url", "")):
            if not self._static:
                await self.refresh_visitor_cookie()
                await asyncio.sleep(2.0)
            raise RateLimitedError("captcha challenge")
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
        match = re.search(r"visitor_gray_callback\((\{.*\})\);?", resp.text)
        if not match:
            raise WeiboError(f"could not parse visitor response: {resp.text[:200]}")
        data = json.loads(match.group(1))
        if data.get("retcode") != 20000000:
            raise WeiboError(f"visitor cookie failed: {data}")
        sub = data["data"]["sub"]
        subp = data["data"]["subp"]
        self._cookie = f"SUB={sub}; SUBP={subp}"
        logger.info("visitor cookie refreshed: tid=%s", data["data"].get("tid"))
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
        except WeiboError as exc:
            logger.warning("extend fetch failed mid=%s: %s", mid, exc)
            return {}
        if data.get("ok") == 1 and isinstance(data.get("data"), dict):
            return data["data"]
        return {}


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
        text_html = str(mblog.get("raw_text") or mblog.get("text_raw") or mblog.get("text") or "")
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
    mid = str(mblog.get("mid") or mblog.get("id") or "")
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
    )
