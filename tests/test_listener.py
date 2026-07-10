import asyncio
import threading
from types import SimpleNamespace

import pytest

from src.config import Settings
from src.listener import (
    CardActionListener,
    ListenerTerminated,
    _parse_forward_event,
)


def action_payload(*, value=None, message_id="om_1"):
    return SimpleNamespace(
        event=SimpleNamespace(
            action=SimpleNamespace(
                value=value
                or {"action": "forward", "mid": "m1", "uid": "42"}
            ),
            context=SimpleNamespace(
                open_message_id=message_id,
                open_chat_id="oc_source",
            ),
            operator=SimpleNamespace(open_id="ou_user"),
        )
    )


def test_parse_forward_event_accepts_dict_and_json_string():
    event = _parse_forward_event(action_payload())
    assert event.mid == "m1"
    assert event.message_id == "om_1"
    assert event.operator_open_id == "ou_user"

    event = _parse_forward_event(
        action_payload(value='{"action":"forward","mid":"m2","uid":"43"}')
    )
    assert event.mid == "m2"
    assert event.uid == "43"


def test_parse_forward_event_rejects_invalid_action():
    assert _parse_forward_event(action_payload(value={"action": "ignore", "mid": "m1"})) is None
    assert _parse_forward_event(action_payload(value={"action": "forward"})) is None
    assert _parse_forward_event(action_payload(message_id="")) is None


@pytest.mark.asyncio
async def test_accepted_action_is_enqueued_with_queue_toast():
    accepted = []

    def accept(event):
        accepted.append(event)
        return True, "已加入转发队列"

    listener = CardActionListener(Settings(), accept)
    response = listener._on_card_action(action_payload(), asyncio.get_running_loop())
    await asyncio.sleep(0)

    assert response.toast.type == "success"
    assert response.toast.content == "已加入转发队列"
    assert accepted[0].mid == "m1"
    assert listener._queue.get_nowait().mid == "m1"


@pytest.mark.asyncio
async def test_ws_exception_returns_to_async_iterator_from_daemon_thread(monkeypatch):
    daemon_flags = []

    class BrokenClient:
        def start(self):
            daemon_flags.append(threading.current_thread().daemon)
            raise RuntimeError("websocket disconnected")

    listener = CardActionListener(Settings(), lambda event: (True, "queued"))
    monkeypatch.setattr(listener, "_build_client", lambda callback: BrokenClient())

    with pytest.raises(ListenerTerminated) as error:
        await anext(listener.listen())

    assert daemon_flags == [True]
    assert isinstance(error.value.__cause__, RuntimeError)
    assert "websocket disconnected" in str(error.value.__cause__)


@pytest.mark.asyncio
async def test_normal_ws_return_is_also_terminal(monkeypatch):
    class ReturningClient:
        def start(self):
            return None

    listener = CardActionListener(Settings(), lambda event: (True, "queued"))
    monkeypatch.setattr(listener, "_build_client", lambda callback: ReturningClient())

    with pytest.raises(ListenerTerminated):
        await anext(listener.listen())
