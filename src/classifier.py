from __future__ import annotations

import json
import logging

import httpx

from .config import Settings
from .models import Post

logger = logging.getLogger(__name__)

LABEL_OPINION = "观点"
LABEL_CONTENT = "内容"
LABEL_AD = "广告"
LABEL_OFFTOPIC = "汽车无关"
LABELS = (LABEL_OPINION, LABEL_CONTENT, LABEL_AD, LABEL_OFFTOPIC)

# 折叠这两类；分类失败/拿不准一律回落到「内容」（宁可放过不要误伤）
FOLDED_LABELS = (LABEL_AD, LABEL_OFFTOPIC)

SYSTEM_PROMPT = """你是汽车行业微博内容分类器。给微博正文打一个标签，四选一：
- 观点：作者对汽车行业、产品、公司、事件表达了明确的个人观点或评价
- 内容：汽车相关的资讯、测评、体验、数据、日常分享等一般内容
- 广告：明显的商业推广、带货、抽奖、软文
- 汽车无关：与汽车行业完全无关的内容

规则：拿不准时一律选「内容」。只有非常确定时才用「广告」或「汽车无关」，宁可放过不要误伤。
只输出 JSON：{"label": "<标签>"}"""


def parse_label(raw: str) -> str:
    """解析模型输出；任何异常回落到「内容」。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return LABEL_CONTENT
    label = str(data.get("label", "")).strip() if isinstance(data, dict) else ""
    return label if label in LABELS else LABEL_CONTENT


def _post_text(post: Post) -> str:
    parts = [post.text_plain.strip()]
    if post.is_repost and post.retweeted_text_plain:
        parts.append(f"（转发自 @{post.retweeted_screen_name}）{post.retweeted_text_plain.strip()}")
    if post.video and post.video.title:
        parts.append(f"（视频：{post.video.title}）")
    return "\n".join(p for p in parts if p)[:2000]


async def classify_post(
    post: Post, settings: Settings, http_client: httpx.AsyncClient
) -> str:
    """给帖子打标签。未启用/无 key/调用失败都返回「内容」，不阻塞推送。"""
    if not settings.classification_enabled or not settings.deepseek_api_key:
        return LABEL_CONTENT
    text = _post_text(post)
    if not text:
        return LABEL_CONTENT

    try:
        resp = await http_client.post(
            f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            json={
                "model": settings.deepseek_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": 50,
            },
            timeout=settings.classify_timeout,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("classification failed mid=%s: %s", post.mid, exc)
        return LABEL_CONTENT

    label = parse_label(raw)
    logger.info("post classified: mid=%s label=%s", post.mid, label)
    return label
