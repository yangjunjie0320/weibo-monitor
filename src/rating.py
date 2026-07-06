from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    ForwardMessageRequest,
    ForwardMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from .card import mark_rated
from .card_store import CardStore
from .config import Settings
from .listener import RateEvent

logger = logging.getLogger(__name__)


class RatingStore:
    """打分记录，JSON 持久化。每条帖子（mid）只记一次分。

    add 在 ws 回调线程调用，pending/mark_synced 在 asyncio 线程调用，加锁互斥。
    结构：{"ratings": {mid: {"score": ..., "synced": false, ...}}}
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._ratings: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("ratings file unreadable, starting fresh: %s: %s", self._path, exc)
            return
        ratings = data.get("ratings")
        if isinstance(ratings, dict):
            self._ratings = ratings

    def add(self, mid: str, record: dict) -> bool:
        """记录一次打分；该 mid 已打过分则返回 False。"""
        with self._lock:
            if mid in self._ratings:
                return False
            self._ratings[mid] = record
            self._save()
            return True

    def is_rated(self, mid: str) -> bool:
        with self._lock:
            return mid in self._ratings

    def pending(self) -> list[tuple[str, dict]]:
        with self._lock:
            return [
                (mid, dict(rec))
                for mid, rec in self._ratings.items()
                if not rec.get("synced")
            ]

    def mark_synced(self, mids: list[str]) -> None:
        with self._lock:
            for mid in mids:
                if mid in self._ratings:
                    self._ratings[mid]["synced"] = True
            self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"ratings": self._ratings}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)


class RatingService:
    """打分事件处理：同步落盘（accept，ws 线程）+ 异步 patch/转发（process）。"""

    def __init__(
        self,
        settings: Settings,
        lark_client: lark.Client,
        card_store: CardStore,
        store: RatingStore,
    ) -> None:
        self._settings = settings
        self._client = lark_client
        self._card_store = card_store
        self._store = store

    def accept(self, event: RateEvent) -> tuple[bool, str]:
        entry = self._card_store.get(event.message_id)
        if entry is None:
            return False, "这张卡片太旧，已不在缓存里，无法打分"
        record = {
            "uid": entry.get("uid", event.uid),
            "screen_name": entry.get("screen_name", ""),
            "label": entry.get("label", ""),
            "summary": entry.get("summary", ""),
            "url": entry.get("url", ""),
            "post_created_at": entry.get("post_created_at", ""),
            "score": event.score,
            "rater_open_id": event.operator_open_id,
            "rated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "synced": False,
        }
        if not self._store.add(event.mid, record):
            return False, "这条已打过分"
        return True, f"已记录：{event.score} 分"

    async def process(self, event: RateEvent) -> None:
        """打分落盘后的动作：patch 原卡片为已打分；三分转发到目标群。"""
        entry = self._card_store.get(event.message_id)
        if entry is None:
            return
        try:
            await self._patch_card(event.message_id, entry["card"], event.score)
        except Exception as e:
            logger.error("patch card failed: mid=%s error=%s", event.mid, e)
        if event.score == 3:
            if not self._settings.forward_chat_id:
                logger.warning("score=3 but forward_chat_id not configured, skip forward")
                return
            try:
                await self._forward(event.message_id)
                logger.info("post forwarded: mid=%s message_id=%s", event.mid, event.message_id)
            except Exception as e:
                logger.error("forward failed: mid=%s error=%s", event.mid, e)

    async def _patch_card(self, message_id: str, card: dict, score: int) -> None:
        content = json.dumps(mark_rated(card, score), ensure_ascii=False)
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(PatchMessageRequestBody.builder().content(content).build())
            .build()
        )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: self._client.im.v1.message.patch(request)
        )
        if not response.success():
            raise RuntimeError(f"patch failed: code={response.code} msg={response.msg}")

    async def _forward(self, message_id: str) -> None:
        request = (
            ForwardMessageRequest.builder()
            .message_id(message_id)
            .receive_id_type("chat_id")
            .request_body(
                ForwardMessageRequestBody.builder()
                .receive_id(self._settings.forward_chat_id)
                .build()
            )
            .build()
        )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: self._client.im.v1.message.forward(request)
        )
        if not response.success():
            raise RuntimeError(f"forward failed: code={response.code} msg={response.msg}")
