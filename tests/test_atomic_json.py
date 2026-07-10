import stat

import pytest

from src.atomic_json import AtomicJsonError, atomic_write_json, load_json_object
from src.card_store import CardStore


def test_atomic_json_roundtrip_and_private_permissions(tmp_path):
    path = tmp_path / "private" / "state.json"
    path.parent.mkdir(mode=0o755)
    atomic_write_json(path, {"value": "中文", "nested": {"ok": True}})

    assert load_json_object(path) == {"value": "中文", "nested": {"ok": True}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_atomic_json_rejects_invalid_utf8(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(AtomicJsonError, match="invalid UTF-8"):
        load_json_object(path)


@pytest.mark.parametrize("content", ["{broken", "[]", "null", '"text"'])
def test_atomic_json_rejects_corrupt_or_non_object_root(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(AtomicJsonError):
        load_json_object(path)
    assert path.read_text(encoding="utf-8") == content


def test_atomic_json_missing_default_is_copied(tmp_path):
    default = {"accounts": {}}
    loaded = load_json_object(tmp_path / "missing.json", default=default)
    loaded["other"] = True
    assert "other" not in default


def test_card_store_rejects_invalid_schema(tmp_path):
    path = tmp_path / "cards.json"
    path.write_text('{"cards": []}', encoding="utf-8")
    with pytest.raises(AtomicJsonError, match="cards must be an object"):
        CardStore(path)
