from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from .config import Settings

logger = logging.getLogger(__name__)


@dataclass
class ForwardEvent:
    mid: str
    uid: str
    message_id: str
    chat_id: str
    operator_open_id: str


class ListenerTerminated(RuntimeError):
    """Raised when the Feishu long-connection thread exits unexpectedly."""


class CardActionListener:
    """Receive card actions through the blocking Feishu WebSocket SDK.

    The SDK is isolated in a daemon thread. Its return value or exception is bridged
    back into the async iterator so the application can fail and let launchd restart
    it. We intentionally do not call private SDK shutdown methods.
    """

    def __init__(
        self, settings: Settings, accept: Callable[[ForwardEvent], tuple[bool, str]]
    ) -> None:
        self._settings = settings
        self._accept = accept
        self._queue: asyncio.Queue[ForwardEvent] = asyncio.Queue()

    async def listen(self) -> AsyncIterator[ForwardEvent]:
        loop = asyncio.get_running_loop()
        ws_client = self._build_client(lambda data: self._on_card_action(data, loop))
        terminated: asyncio.Future[None] = loop.create_future()
        # The daemon can outlive cancellation of this iterator. Always retrieve its
        # exception to avoid an orphaned-future warning when the event loop shuts down.
        terminated.add_done_callback(lambda future: future.exception())

        def run_client() -> None:
            try:
                ws_client.start()
            except BaseException as exc:
                self._notify_terminated(loop, terminated, exc)
            else:
                self._notify_terminated(
                    loop,
                    terminated,
                    ListenerTerminated("Feishu long connection returned unexpectedly"),
                )

        thread = threading.Thread(
            target=run_client,
            name="feishu-card-listener",
            daemon=True,
        )
        thread.start()
        logger.info("card action listener: ws long connection starting")

        while True:
            queue_get = asyncio.create_task(self._queue.get())
            try:
                done, _ = await asyncio.wait(
                    {queue_get, terminated}, return_when=asyncio.FIRST_COMPLETED
                )
            except BaseException:
                queue_get.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await queue_get
                raise
            if terminated in done:
                queue_get.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await queue_get
                # Calling result re-raises the original SDK exception. Wrap it in a
                # stable application exception while retaining the cause.
                try:
                    terminated.result()
                except BaseException as exc:
                    raise ListenerTerminated("Feishu long connection stopped") from exc
                raise ListenerTerminated("Feishu long connection stopped")
            yield queue_get.result()

    @staticmethod
    def _notify_terminated(
        loop: asyncio.AbstractEventLoop,
        future: asyncio.Future[None],
        error: BaseException,
    ) -> None:
        def set_error() -> None:
            if not future.done():
                future.set_exception(error)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(set_error)

    def _build_client(
        self,
        on_card_action: Callable[[P2CardActionTrigger], P2CardActionTriggerResponse],
    ) -> Any:
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_card_action_trigger(on_card_action)
            .build()
        )
        return lark.ws.Client(
            self._settings.app_id,
            self._settings.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
        )

    def _on_card_action(
        self,
        data: P2CardActionTrigger,
        loop: asyncio.AbstractEventLoop,
    ) -> P2CardActionTriggerResponse:
        event = _parse_forward_event(data)
        if event is None:
            raw_event = getattr(data, "event", None)
            raw = getattr(getattr(raw_event, "action", None), "value", None)
            logger.warning("card action ignored: unrecognized payload: value=%r", raw)
            return P2CardActionTriggerResponse(
                {"toast": {"type": "warning", "content": "无法识别这个按钮动作"}}
            )
        ok, toast = self._accept(event)
        logger.info(
            "card action: mid=%s accepted=%s operator=%s",
            event.mid,
            ok,
            event.operator_open_id,
        )
        if ok:
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, event)
            except RuntimeError:
                logger.error("card action accepted while application loop is unavailable")
                ok = False
                toast = "服务正在重启，请稍后重试"
        return P2CardActionTriggerResponse(
            {"toast": {"type": "success" if ok else "info", "content": toast}}
        )


def _parse_forward_event(data: P2CardActionTrigger) -> ForwardEvent | None:
    try:
        event = data.event
        if event is None or event.action is None:
            return None
        value = _coerce_action_value(event.action.value)
        if str(value.get("action") or "") != "forward":
            return None
        mid = str(value.get("mid") or "")
        if not mid:
            return None

        context = event.context
        message_id = str(getattr(context, "open_message_id", "") or "")
        chat_id = str(getattr(context, "open_chat_id", "") or "")
        if not message_id:
            logger.warning("card action missing message_id: mid=%s", mid)
            return None

        operator = event.operator
        return ForwardEvent(
            mid=mid,
            uid=str(value.get("uid") or ""),
            message_id=message_id,
            chat_id=chat_id,
            operator_open_id=str(getattr(operator, "open_id", "") or ""),
        )
    except (AttributeError, ValueError) as exc:
        logger.warning("failed to parse card action event: %s", exc)
        return None


def _coerce_action_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
