from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class StateStore:
    """按 uid 记录已见过的 mid，JSON 文件持久化，原子写。

    结构：{"accounts": {uid: {"mids": [最新在前], "last_poll": iso}}}
    """

    def __init__(self, path: str | Path, keep_per_account: int = 200) -> None:
        self._path = Path(path)
        self._keep = keep_per_account
        self._accounts: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("state file unreadable, starting fresh: %s: %s", self._path, exc)
            return
        accounts = data.get("accounts")
        if isinstance(accounts, dict):
            self._accounts = accounts

    def has_account(self, uid: str) -> bool:
        return uid in self._accounts

    def is_seen(self, uid: str, mid: str) -> bool:
        entry = self._accounts.get(uid)
        return bool(entry) and mid in entry.get("mids", [])

    def mark_seen(self, uid: str, mids: list[str], *, last_poll: str = "") -> None:
        entry = self._accounts.setdefault(uid, {"mids": []})
        existing = entry.get("mids", [])
        fresh = [mid for mid in mids if mid not in existing]
        entry["mids"] = (fresh + existing)[: self._keep]
        if last_poll:
            entry["last_poll"] = last_poll

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"accounts": self._accounts}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
