import stat

import pytest

from src.atomic_json import AtomicJsonError
from src.state import StateStore


def test_roundtrip(tmp_path):
    path = tmp_path / "seen.json"
    store = StateStore(path, keep_per_account=200)
    assert not store.has_account("1")

    store.mark_seen("1", ["m1", "m2"], last_poll="2026-07-02T00:00:00")
    store.save()

    reloaded = StateStore(path)
    assert reloaded.has_account("1")
    assert reloaded.is_seen("1", "m1")
    assert reloaded.is_seen("1", "m2")
    assert not reloaded.is_seen("1", "m3")


def test_keep_limit_evicts_oldest(tmp_path):
    store = StateStore(tmp_path / "seen.json", keep_per_account=3)
    store.mark_seen("1", ["a", "b", "c"])
    store.mark_seen("1", ["d", "e"])
    assert store.is_seen("1", "d")
    assert store.is_seen("1", "e")
    assert store.is_seen("1", "a")
    assert not store.is_seen("1", "b")  # 最老的被挤出
    assert not store.is_seen("1", "c")


def test_mark_seen_dedupes(tmp_path):
    store = StateStore(tmp_path / "seen.json", keep_per_account=5)
    store.mark_seen("1", ["a", "b"])
    store.mark_seen("1", ["b", "c"])
    store.mark_seen("1", [])  # 只刷 last_poll 的空调用不应清空
    assert store.is_seen("1", "a")
    assert store.is_seen("1", "b")
    assert store.is_seen("1", "c")


def test_corrupt_file_fails_without_overwriting(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(AtomicJsonError, match="invalid JSON"):
        StateStore(path)
    assert path.read_text(encoding="utf-8") == "{not json"


def test_read_only_store_never_writes(tmp_path):
    path = tmp_path / "seen.json"
    store = StateStore(path, read_only=True)
    store.mark_seen("1", ["a"], last_poll="2026-07-11T00:00:00")
    assert store.is_seen("1", "a")
    store.save()
    assert not path.exists()


def test_state_file_is_private(tmp_path):
    path = tmp_path / "state" / "seen.json"
    store = StateStore(path)
    store.mark_seen("1", ["a"])
    store.save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
