from __future__ import annotations

import lark_oapi as lark
from lark_oapi.api.im.v1 import ListChatRequest


def list_chats(client: lark.Client) -> list[tuple[str, str]]:
    """列出机器人所在的所有群，返回 (chat_id, name)。"""
    chats: list[tuple[str, str]] = []
    page_token = ""
    while True:
        builder = ListChatRequest.builder().page_size(100)
        if page_token:
            builder = builder.page_token(page_token)
        response = client.im.v1.chat.list(builder.build())
        if not response.success():
            raise RuntimeError(f"chat list failed: code={response.code} msg={response.msg}")
        for item in response.data.items or []:
            chats.append((item.chat_id or "", item.name or "(未命名)"))
        if not response.data.has_more:
            break
        page_token = response.data.page_token or ""
    return chats
