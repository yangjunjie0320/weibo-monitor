from __future__ import annotations

import asyncio
import json
import logging
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
class RateEvent:
    score: int
    mid: str
    uid: str
    message_id: str
    chat_id: str
    operator_open_id: str


class CardActionListener:
    """WebSocket 长连接接收卡片按钮回调（无需公网地址），移植自 feishu-link。

    accept 在 ws 线程内同步调用（回调 3 秒内必须应答，只能做轻量校验+落盘），
    返回 (是否接受, toast 文案)；接受的事件进队列，由 listen() 消费方异步处理。
    """

    def __init__(
        self, settings: Settings, accept: Callable[[RateEvent], tuple[bool, str]]
    ) -> None:
        self._settings = settings
        self._accept = accept
        self._queue: asyncio.Queue[RateEvent] = asyncio.Queue()

    async def listen(self) -> AsyncIterator[RateEvent]:
        loop = asyncio.get_running_loop()

        def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
            event = _parse_rate_event(data)
            if event is None:
                logger.warning("card action ignored: unrecognized payload")
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "warning", "content": "无法识别这个按钮动作"}}
                )
            ok, toast = self._accept(event)
            logger.info(
                "card action: mid=%s score=%d accepted=%s operator=%s",
                event.mid,
                event.score,
                ok,
                event.operator_open_id,
            )
            if ok:
                loop.call_soon_threadsafe(self._queue.put_nowait, event)
            return P2CardActionTriggerResponse(
                {"toast": {"type": "success" if ok else "info", "content": toast}}
            )

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_card_action_trigger(on_card_action)
            .build()
        )
        ws_client = lark.ws.Client(
            self._settings.app_id,
            self._settings.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
        )
        loop.run_in_executor(None, ws_client.start)
        logger.info("card action listener: ws long connection starting")

        while True:
            yield await self._queue.get()


def _parse_rate_event(data: P2CardActionTrigger) -> RateEvent | None:
    try:
        event = data.event
        if event is None or event.action is None:
            return None
        value = _coerce_action_value(event.action.value)
        if str(value.get("action") or "") != "rate":
            return None
        score = int(value.get("score") or 0)
        mid = str(value.get("mid") or "")
        if score not in (1, 2, 3) or not mid:
            return None

        context = event.context
        message_id = str(getattr(context, "open_message_id", "") or "")
        chat_id = str(getattr(context, "open_chat_id", "") or "")
        if not message_id:
            logger.warning("card action missing message_id: mid=%s", mid)
            return None

        operator = event.operator
        return RateEvent(
            score=score,
            mid=mid,
            uid=str(value.get("uid") or ""),
            message_id=message_id,
            chat_id=chat_id,
            operator_open_id=str(getattr(operator, "open_id", "") or ""),
        )
    except (AttributeError, ValueError) as e:
        logger.warning("failed to parse card action event: %s", e)
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
