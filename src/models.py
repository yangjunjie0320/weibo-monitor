from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


class Account(BaseModel):
    name: str
    uid: str


class VideoInfo(BaseModel):
    object_id: str = ""
    title: str = ""
    duration: float | None = None
    play_count: str = ""


class Post(BaseModel):
    """统一数据契约：字段缺失留空而非占位。"""

    uid: str
    screen_name: str = ""
    mid: str
    bid: str = ""
    created_at: dt.datetime
    is_pinned: bool = False
    is_repost: bool = False
    text_html: str = ""
    text_plain: str = ""
    source: str = ""
    region_name: str = ""
    reposts_count: int = 0
    comments_count: int = 0
    attitudes_count: int = 0
    image_urls: list[str] = []
    video: VideoInfo | None = None
    retweeted_screen_name: str = ""
    retweeted_text_plain: str = ""

    @property
    def url(self) -> str:
        return f"https://weibo.com/{self.uid}/{self.bid or self.mid}"
