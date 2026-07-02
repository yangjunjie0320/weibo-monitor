from __future__ import annotations

import asyncio
import io
import logging

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

logger = logging.getLogger(__name__)

# 微博图床（sinaimg.cn）有防盗链，必须带 weibo Referer 才能下载
WEIBO_IMAGE_HEADERS = {
    "Referer": "https://weibo.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
}


async def upload_image(
    url: str,
    lark_client: lark.Client,
    http_client: httpx.AsyncClient,
) -> str | None:
    """下载微博图片并上传飞书，返回 image_key；任何失败返回 None（不阻塞发卡）。"""
    try:
        resp = await http_client.get(
            url, headers=WEIBO_IMAGE_HEADERS, follow_redirects=True, timeout=15
        )
        resp.raise_for_status()
        image_bytes = resp.content
    except Exception as e:
        logger.warning("image fetch failed url=%s: %s", url, e)
        return None

    def _upload() -> str | None:
        try:
            request = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(io.BytesIO(image_bytes))
                    .build()
                )
                .build()
            )
            response = lark_client.im.v1.image.create(request)
            if response.success():
                return response.data.image_key
            logger.warning("image upload failed: %s %s", response.code, response.msg)
            return None
        except Exception as e:
            logger.warning("image upload failed url=%s: %s", url, e)
            return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _upload)
