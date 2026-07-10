from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import threading
from pathlib import Path
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    ForwardMessageRequest,
    ForwardMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from .atomic_json import atomic_write_json, load_json_object
from .card import mark_forwarded
from .card_store import CardStore
from .config import Settings
from .listener import ForwardEvent

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
RETRY_DELAYS_SECONDS = (60, 300, 900, 3600, 14400)
_DELIVERY_STATUSES = {"queued", "forwarded", "failed"}
_CARD_STATUSES = {"pending", "patched", "failed"}
_ARCHIVE_STATUSES = {"pending", "synced", "failed"}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat(timespec="seconds")


def _parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _error_text(error: BaseException | str) -> str:
    # SDK errors can be unexpectedly verbose. Keep state files useful and bounded.
    return str(error).replace("\n", " ")[:500]


class ForwardStore:
    """Thread-safe schema-v2 delivery, card patch, and archive state.

    The WebSocket callback writes from the SDK thread while retry and archive workers
    write from asyncio/executor threads, so every read-modify-write is protected by a
    single lock. A legacy file is migrated on load and immediately persisted as v2.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._forwards: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        data = load_json_object(self._path)
        forwards = data.get("forwards", {})
        if not isinstance(forwards, dict):
            raise ValueError(f"forward store 'forwards' must be an object: {self._path}")

        version = data.get("schema_version")
        if version not in {None, 1, SCHEMA_VERSION}:
            raise ValueError(f"unsupported forward store schema: {version!r}")
        legacy = version != SCHEMA_VERSION
        for mid, raw in forwards.items():
            if not isinstance(raw, dict):
                raise ValueError(f"forward record must be an object: mid={mid}")
            self._forwards[str(mid)] = self._normalise_record(raw, legacy=legacy)
        if legacy:
            self._save()

    @staticmethod
    def _normalise_record(raw: dict[str, Any], *, legacy: bool) -> dict[str, Any]:
        record = dict(raw)
        if legacy:
            # A v1 record was created before the actual API result was known. Treat it
            # as delivered so migration can never repeat a potentially completed send.
            synced = bool(record.pop("synced", False))
            record["delivery_status"] = "forwarded"
            record["card_status"] = "patched"
            record["archive_status"] = "synced" if synced else "pending"

        statuses = (
            ("delivery_status", _DELIVERY_STATUSES),
            ("card_status", _CARD_STATUSES),
            ("archive_status", _ARCHIVE_STATUSES),
        )
        for field, allowed in statuses:
            if record.get(field) not in allowed:
                raise ValueError(f"invalid forward {field}: {record.get(field)!r}")

        record.setdefault("attempts", 0)
        record.setdefault("next_attempt_at", None)
        record.setdefault("last_error", None)
        record.setdefault("requested_at", "")
        record.setdefault("forwarded_at", "")
        record.setdefault("card_attempts", 0)
        record.setdefault("card_next_attempt_at", None)
        record.setdefault("card_last_error", None)
        record.setdefault("card_patched_at", "")
        record.setdefault("archive_attempts", 0)
        record.setdefault("archive_next_attempt_at", None)
        record.setdefault("archive_last_error", None)
        record.setdefault("archived_at", "")
        for field in ("attempts", "card_attempts", "archive_attempts"):
            if not isinstance(record[field], int) or record[field] < 0:
                raise ValueError(f"invalid forward {field}: {record[field]!r}")
        return record

    def enqueue(
        self,
        mid: str,
        record: dict[str, Any],
        *,
        now: dt.datetime | None = None,
    ) -> str:
        """Queue a new send or manually retry an exhausted failure.

        Returns ``created``, ``requeued``, ``queued``, or ``forwarded``. A delivered
        record is never requeued, even when card patching or archiving is still pending.
        """

        now = now or _utcnow()
        with self._lock:
            existing = self._forwards.get(mid)
            if existing is not None:
                status = existing["delivery_status"]
                if status == "forwarded":
                    return "forwarded"
                if status == "queued":
                    return "queued"
                if existing.get("next_attempt_at"):
                    return "queued"
                # A fresh click explicitly retries a delivery that exhausted retries.
                existing.update(record)
                existing.update(self._new_state(now))
                self._save()
                return "requeued"

            item = dict(record)
            item.update(self._new_state(now))
            self._forwards[mid] = self._normalise_record(item, legacy=False)
            self._save()
            return "created"

    @staticmethod
    def _new_state(now: dt.datetime) -> dict[str, Any]:
        requested_at = _iso(now)
        return {
            "delivery_status": "queued",
            "card_status": "pending",
            "archive_status": "pending",
            "attempts": 0,
            "next_attempt_at": requested_at,
            "last_error": None,
            "requested_at": requested_at,
            "forwarded_at": "",
            "card_attempts": 0,
            "card_next_attempt_at": None,
            "card_last_error": None,
            "card_patched_at": "",
            "archive_attempts": 0,
            "archive_next_attempt_at": None,
            "archive_last_error": None,
            "archived_at": "",
        }

    def get(self, mid: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._forwards.get(mid)
            return dict(record) if record is not None else None

    def records(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {mid: dict(record) for mid, record in self._forwards.items()}

    def due_delivery(
        self, *, now: dt.datetime | None = None
    ) -> list[tuple[str, dict[str, Any]]]:
        now = (now or _utcnow()).astimezone(dt.UTC)
        with self._lock:
            return [
                (mid, dict(record))
                for mid, record in self._forwards.items()
                if record["delivery_status"] in {"queued", "failed"}
                and self._is_due(
                    record.get("next_attempt_at"),
                    now,
                    missing_is_due=record["delivery_status"] == "queued",
                )
            ]

    def due_cards(
        self, *, now: dt.datetime | None = None
    ) -> list[tuple[str, dict[str, Any]]]:
        now = (now or _utcnow()).astimezone(dt.UTC)
        with self._lock:
            due: list[tuple[str, dict[str, Any]]] = []
            for mid, record in self._forwards.items():
                if record["delivery_status"] != "forwarded":
                    continue
                status = record["card_status"]
                if status in {"pending", "failed"} and self._is_due(
                    record.get("card_next_attempt_at"),
                    now,
                    missing_is_due=status == "pending",
                ):
                    due.append((mid, dict(record)))
            return due

    def pending_archive(
        self, *, now: dt.datetime | None = None
    ) -> list[tuple[str, dict[str, Any]]]:
        now = (now or _utcnow()).astimezone(dt.UTC)
        with self._lock:
            pending: list[tuple[str, dict[str, Any]]] = []
            for mid, record in self._forwards.items():
                if (
                    record["delivery_status"] != "forwarded"
                    or record["card_status"] != "patched"
                ):
                    continue
                status = record["archive_status"]
                if status == "pending" or (
                    status == "failed"
                    and self._is_due(
                        record.get("archive_next_attempt_at"), now, missing_is_due=True
                    )
                ):
                    pending.append((mid, dict(record)))
            return pending

    @staticmethod
    def _is_due(value: object, now: dt.datetime, *, missing_is_due: bool) -> bool:
        parsed = _parse_time(value)
        return missing_is_due if parsed is None else parsed <= now

    def mark_delivery_forwarded(
        self, mid: str, *, now: dt.datetime | None = None
    ) -> None:
        now = now or _utcnow()
        with self._lock:
            record = self._require(mid)
            record.update(
                delivery_status="forwarded",
                forwarded_at=_iso(now),
                next_attempt_at=None,
                last_error=None,
            )
            self._save()

    def mark_delivery_failed(
        self,
        mid: str,
        error: BaseException | str,
        *,
        now: dt.datetime | None = None,
    ) -> None:
        now = now or _utcnow()
        with self._lock:
            record = self._require(mid)
            attempts = int(record.get("attempts") or 0) + 1
            record["attempts"] = attempts
            record["last_error"] = _error_text(error)
            record["delivery_status"] = "failed"
            if attempts <= len(RETRY_DELAYS_SECONDS):
                record["next_attempt_at"] = _iso(
                    now + dt.timedelta(seconds=RETRY_DELAYS_SECONDS[attempts - 1])
                )
            else:
                record["next_attempt_at"] = None
            self._save()

    def mark_card_patched(self, mid: str, *, now: dt.datetime | None = None) -> None:
        now = now or _utcnow()
        with self._lock:
            record = self._require(mid)
            record.update(
                card_status="patched",
                card_patched_at=_iso(now),
                card_next_attempt_at=None,
                card_last_error=None,
            )
            self._save()

    def mark_card_failed(
        self,
        mid: str,
        error: BaseException | str,
        *,
        now: dt.datetime | None = None,
    ) -> None:
        now = now or _utcnow()
        with self._lock:
            record = self._require(mid)
            attempts = int(record.get("card_attempts") or 0) + 1
            record["card_attempts"] = attempts
            record["card_status"] = "failed"
            record["card_last_error"] = _error_text(error)
            if attempts <= len(RETRY_DELAYS_SECONDS):
                record["card_next_attempt_at"] = _iso(
                    now + dt.timedelta(seconds=RETRY_DELAYS_SECONDS[attempts - 1])
                )
            else:
                record["card_next_attempt_at"] = None
            self._save()

    def mark_archive_synced(
        self, mids: list[str], *, now: dt.datetime | None = None
    ) -> None:
        if not mids:
            return
        now = now or _utcnow()
        with self._lock:
            for mid in mids:
                record = self._forwards.get(mid)
                if record is None:
                    continue
                record.update(
                    archive_status="synced",
                    archived_at=_iso(now),
                    archive_next_attempt_at=None,
                    archive_last_error=None,
                )
            self._save()

    def mark_archive_failed(
        self,
        mids: list[str],
        error: BaseException | str,
        *,
        retry_after_seconds: float,
        now: dt.datetime | None = None,
    ) -> None:
        if not mids:
            return
        now = now or _utcnow()
        with self._lock:
            for mid in mids:
                record = self._forwards.get(mid)
                if record is None or record["archive_status"] == "synced":
                    continue
                record["archive_status"] = "failed"
                record["archive_attempts"] = int(record.get("archive_attempts") or 0) + 1
                record["archive_last_error"] = _error_text(error)
                record["archive_next_attempt_at"] = _iso(
                    now + dt.timedelta(seconds=max(0, retry_after_seconds))
                )
            self._save()

    # Compatibility aliases retained for small external maintenance scripts.
    def pending(self) -> list[tuple[str, dict[str, Any]]]:
        return self.pending_archive()

    def mark_synced(self, mids: list[str]) -> None:
        self.mark_archive_synced(mids)

    def _require(self, mid: str) -> dict[str, Any]:
        try:
            return self._forwards[mid]
        except KeyError as exc:
            raise KeyError(f"unknown forward mid: {mid}") from exc

    def _save(self) -> None:
        atomic_write_json(
            self._path,
            {"schema_version": SCHEMA_VERSION, "forwards": self._forwards},
        )


class ForwardService:
    """Durable delivery worker for accepted card actions.

    ``accept`` is synchronous and only validates plus queues. ``process`` performs an
    immediate attempt for a newly accepted event. ``run_forever`` must also run as a
    background task so retries and work recovered after a restart are processed.
    """

    def __init__(
        self,
        settings: Settings,
        lark_client: lark.Client,
        card_store: CardStore,
        store: ForwardStore,
        *,
        retry_poll_seconds: float = 5.0,
    ) -> None:
        self._settings = settings
        self._client = lark_client
        self._card_store = card_store
        self._store = store
        self._retry_poll_seconds = retry_poll_seconds
        self._mid_locks: dict[str, asyncio.Lock] = {}

    def accept(self, event: ForwardEvent) -> tuple[bool, str]:
        entry = self._card_store.get(event.message_id)
        if entry is None:
            return False, "这张卡片太旧，已不在缓存里，无法转发"
        if str(entry.get("mid") or "") != event.mid:
            logger.warning(
                "forward action rejected: message/mid mismatch message_id=%s action_mid=%s",
                event.message_id,
                event.mid,
            )
            return False, "卡片信息不匹配，已拒绝转发"

        record: dict[str, Any] = {
            "message_id": event.message_id,
            "uid": entry.get("uid", event.uid),
            "screen_name": entry.get("screen_name", ""),
            "label": entry.get("label", ""),
            "summary": entry.get("summary", ""),
            "url": entry.get("url", ""),
            "post_created_at": entry.get("post_created_at", ""),
            "forwarder_open_id": event.operator_open_id,
            # Persist the card so patch retries still work after CardStore eviction.
            "card": entry.get("card", {}),
        }
        result = self._store.enqueue(event.mid, record)
        if result in {"created", "requeued"}:
            return True, "已加入转发队列"
        if result == "queued":
            return False, "这条已在转发队列中"
        return False, "这条已转发过"

    async def process(self, event: ForwardEvent) -> None:
        """Immediately process a just-accepted event; failures remain queued."""

        await self._process_mid(event.mid)

    async def run_forever(self) -> None:
        """Recover persisted work and execute due delivery/card retries."""

        while True:
            await self.process_due()
            await asyncio.sleep(self._retry_poll_seconds)

    async def process_due(self) -> None:
        for mid, _ in self._store.due_delivery():
            await self._process_mid(mid)
        for mid, _ in self._store.due_cards():
            await self._process_card(mid)

    async def _process_mid(self, mid: str) -> None:
        lock = self._mid_locks.setdefault(mid, asyncio.Lock())
        async with lock:
            record = self._store.get(mid)
            if record is None or record["delivery_status"] not in {"queued", "failed"}:
                return
            next_attempt = _parse_time(record.get("next_attempt_at"))
            if next_attempt is not None and next_attempt > _utcnow():
                return
            try:
                if not self._settings.forward_chat_id:
                    raise RuntimeError("forward_chat_id not configured")
                await self._forward(str(record.get("message_id") or ""))
            except Exception as exc:
                self._store.mark_delivery_failed(mid, exc)
                current = self._store.get(mid) or {}
                logger.warning(
                    "forward failed: mid=%s attempts=%s next_attempt_at=%s error=%s",
                    mid,
                    current.get("attempts"),
                    current.get("next_attempt_at"),
                    _error_text(exc),
                )
                return

            # Persist delivery before any ancillary operation. Card or archive failure
            # must never cause a second delivery.
            self._store.mark_delivery_forwarded(mid)
            logger.info("post forwarded: mid=%s message_id=%s", mid, record.get("message_id"))
            await self._process_card_locked(mid)

    async def _process_card(self, mid: str) -> None:
        lock = self._mid_locks.setdefault(mid, asyncio.Lock())
        async with lock:
            await self._process_card_locked(mid)

    async def _process_card_locked(self, mid: str) -> None:
        record = self._store.get(mid)
        if (
            record is None
            or record["delivery_status"] != "forwarded"
            or record["card_status"] == "patched"
        ):
            return
        card_next_attempt = _parse_time(record.get("card_next_attempt_at"))
        if card_next_attempt is not None and card_next_attempt > _utcnow():
            return
        try:
            card = record.get("card")
            if not isinstance(card, dict) or not card:
                raise RuntimeError("original card unavailable")
            await self._patch_card(str(record.get("message_id") or ""), card)
        except Exception as exc:
            self._store.mark_card_failed(mid, exc)
            current = self._store.get(mid) or {}
            logger.warning(
                "patch card failed: mid=%s attempts=%s next_attempt_at=%s error=%s",
                mid,
                current.get("card_attempts"),
                current.get("card_next_attempt_at"),
                _error_text(exc),
            )
            return
        self._store.mark_card_patched(mid)

    async def _patch_card(self, message_id: str, card: dict[str, Any]) -> None:
        content = json.dumps(mark_forwarded(card), ensure_ascii=False)
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
        if not message_id:
            raise RuntimeError("message_id is missing")
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
