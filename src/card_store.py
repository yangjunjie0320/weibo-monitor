from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class CardStore:
    """已发卡片按 message_id 缓存，供打分回调 patch 原卡片、关联回帖子。

    结构：{"cards": {message_id: {"card": dict, "mid": ..., ...}}}，
    依赖 dict 插入序做 FIFO，超上限丢最旧的（超龄卡片不需要还能打分）。
    """

    def __init__(self, path: str | Path, max_entries: int = 500) -> None:
        self._path = Path(path)
        self._max = max_entries
        self._cards: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("card store unreadable, starting fresh: %s: %s", self._path, exc)
            return
        cards = data.get("cards")
        if isinstance(cards, dict):
            self._cards = cards

    def put(self, message_id: str, entry: dict) -> None:
        self._cards.pop(message_id, None)
        self._cards[message_id] = entry
        while len(self._cards) > self._max:
            oldest = next(iter(self._cards))
            del self._cards[oldest]
        self._save()

    def get(self, message_id: str) -> dict | None:
        return self._cards.get(message_id)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"cards": self._cards}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
