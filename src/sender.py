from __future__ import annotations

import asyncio
import json
import logging

import httpx
import lark_oapi as lark
import tenacity
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from .card import build_post_card
from .card_store import CardStore
from .classifier import classify_post
from .config import Settings
from .image_uploader import upload_image
from .models import Post

logger = logging.getLogger(__name__)


class SendError(Exception):
    pass


class CardSender:
    def __init__(self, settings: Settings, client: lark.Client) -> None:
        self._settings = settings
        self._client = client

    async def send(self, card_json: str) -> str | None:
        """发送卡片，成功返回 message_id，失败返回 None。"""

        @tenacity.retry(
            stop=tenacity.stop_after_attempt(self._settings.send_retry_attempts),
            wait=tenacity.wait_exponential(multiplier=1, min=1, max=9),
            reraise=True,
        )
        async def _attempt() -> str:
            return await self._create(card_json)

        try:
            return await _attempt()
        except Exception as e:
            logger.critical("card send exhausted all retries: error=%s", e)
            return None

    async def _create(self, card_json: str) -> str:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(self._settings.chat_id)
                .msg_type("interactive")
                .content(card_json)
                .build()
            )
            .build()
        )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: self._client.im.v1.message.create(request)
        )
        if not response.success():
            raise SendError(f"create failed: code={response.code} msg={response.msg}")
        return response.data.message_id or ""


class PostPusher:
    """单条新帖的推送流水线：传首图（尽力）→ 组卡片 → 发送。"""

    def __init__(
        self,
        settings: Settings,
        lark_client: lark.Client,
        http_client: httpx.AsyncClient,
        *,
        card_store: CardStore | None = None,
        dry_run: bool = False,
    ) -> None:
        self._settings = settings
        self._lark_client = lark_client
        self._http_client = http_client
        self._sender = CardSender(settings, lark_client)
        self._card_store = card_store
        self._dry_run = dry_run

    async def push(self, post: Post) -> bool:
        result = await classify_post(post, self._settings, self._http_client)
        if result.should_drop(self._settings):
            # 视为已处理（返回 True 落 state），不再重试
            logger.info(
                "post dropped: name=%s mid=%s label=%s china=%s url=%s",
                post.screen_name,
                post.mid,
                result.label,
                result.china,
                post.url,
            )
            return True

        image_key = None
        if post.image_urls and not self._dry_run:
            image_key = await upload_image(
                post.image_urls[0], self._lark_client, self._http_client
            )
        card = build_post_card(
            post, image_key, result.label, with_rating=self._settings.rating_enabled
        )

        if self._dry_run:
            logger.info(
                "[dry-run] would push: name=%s mid=%s label=%s url=%s text=%s",
                post.screen_name,
                post.mid,
                result.label,
                post.url,
                post.text_plain[:80].replace("\n", " "),
            )
            return True

        message_id = await self._sender.send(json.dumps(card, ensure_ascii=False))
        if message_id is None:
            return False
        logger.info(
            "post pushed: name=%s mid=%s url=%s", post.screen_name, post.mid, post.url
        )
        if self._card_store is not None and message_id:
            self._card_store.put(
                message_id,
                {
                    "card": card,
                    "mid": post.mid,
                    "uid": post.uid,
                    "screen_name": post.screen_name,
                    "label": result.label,
                    "summary": post.text_plain.strip()[:100],
                    "url": post.url,
                    "post_created_at": post.created_at.isoformat(timespec="seconds"),
                },
            )
        return True
