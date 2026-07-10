from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any


class AtomicJsonError(RuntimeError):
    """JSON state could not be read or written safely."""


def load_json_object(
    path: str | Path,
    *,
    default: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load a JSON object, returning ``default`` only when the file is absent.

    Corrupt files and non-object roots are deliberately fatal.  Silently replacing
    either with empty state could resend old posts or duplicate forwarded messages.
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {} if default is None else default.copy()
    except UnicodeDecodeError as exc:
        raise AtomicJsonError(f"invalid UTF-8 JSON state {target}: {exc}") from exc
    except OSError as exc:
        raise AtomicJsonError(f"cannot read JSON state {target}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AtomicJsonError(f"invalid JSON state {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise AtomicJsonError(f"JSON state root must be an object: {target}")
    return data


def atomic_write_json(
    path: str | Path,
    data: Mapping[str, Any],
    mode: int = 0o600,
) -> None:
    """Durably replace a JSON object with a private, same-directory temp file."""
    if not isinstance(data, Mapping):
        raise AtomicJsonError("JSON state root must be an object")

    target = Path(path)
    try:
        payload = json.dumps(dict(data), ensure_ascii=False, indent=1).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AtomicJsonError(f"cannot encode JSON state {target}: {exc}") from exc

    parent = target.parent
    try:
        _ensure_private_directory(parent)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=parent
        )
    except OSError as exc:
        raise AtomicJsonError(f"cannot create JSON state {target}: {exc}") from exc

    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, mode)
        _fsync_directory(parent)
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise AtomicJsonError(f"cannot write JSON state {target}: {exc}") from exc


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = path.resolve()
    protected_roots = {Path.cwd().resolve(), Path.home().resolve(), Path(resolved.anchor)}
    directory_mode = path.stat().st_mode
    if resolved not in protected_roots and not directory_mode & stat.S_ISVTX:
        os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    """Persist the rename itself where the platform supports directory fsync."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
