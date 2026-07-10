from __future__ import annotations

from pathlib import Path

from .atomic_json import AtomicJsonError, atomic_write_json, load_json_object


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
        data = load_json_object(self._path, default={"cards": {}})
        cards = data.get("cards", {})
        if not isinstance(cards, dict):
            raise AtomicJsonError(f"card store cards must be an object: {self._path}")
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
        atomic_write_json(self._path, {"cards": self._cards})
