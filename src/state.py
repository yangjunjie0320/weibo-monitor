from __future__ import annotations

from pathlib import Path

from .atomic_json import AtomicJsonError, atomic_write_json, load_json_object


class StateStore:
    """按 uid 记录已见过的 mid，JSON 文件持久化，原子写。

    结构：{"accounts": {uid: {"mids": [最新在前], "last_poll": iso}}}
    """

    def __init__(
        self,
        path: str | Path,
        keep_per_account: int = 200,
        *,
        read_only: bool = False,
    ) -> None:
        self._path = Path(path)
        self._keep = keep_per_account
        self._read_only = read_only
        self._accounts: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        data = load_json_object(self._path, default={"accounts": {}})
        accounts = data.get("accounts", {})
        if not isinstance(accounts, dict):
            raise AtomicJsonError(f"state accounts must be an object: {self._path}")
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
        if self._read_only:
            return
        atomic_write_json(self._path, {"accounts": self._accounts})
