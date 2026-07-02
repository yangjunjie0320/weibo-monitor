from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from .config import Settings
from .models import Post

logger = logging.getLogger(__name__)

# 业务分类（来自编辑团队的分类清单）
CATEGORIES = (
    "车圈热点",
    "产品发布",
    "谍照申报",
    "市场数据",
    "资本市场",
    "出海信息",
    "政策监管",
    "行业观察",
)
LABEL_AD = "广告"
LABEL_OFFTOPIC = "汽车无关"
LABELS = (*CATEGORIES, LABEL_AD, LABEL_OFFTOPIC)

# 分类失败/拿不准的回落值（正常展示，宁可放过不要误伤）
DEFAULT_LABEL = "行业观察"


@dataclass
class Classification:
    label: str = DEFAULT_LABEL
    china: bool = True

    def should_drop(self, settings: Settings) -> bool:
        if settings.drop_offtopic and self.label == LABEL_OFFTOPIC:
            return True
        if settings.drop_ads and self.label == LABEL_AD:
            return True
        return bool(settings.drop_non_china and not self.china)


SYSTEM_PROMPT = """你是中国汽车行业微博内容分类器，为面向海外读者的中国汽车资讯编辑部筛选素材。

输出两个字段：

label，十选一：
- 车圈热点：行业热点事件、舆论焦点、突发新闻
- 产品发布：新车发布、上市、改款、配置与定价信息
- 谍照申报：谍照、工信部申报图、未发布车型情报
- 市场数据：销量、交付量、市场份额、价格走势等数据
- 资本市场：融资、股价、IPO、并购、财报、组织与资本变动
- 出海信息：中国车企在海外市场的动态
- 政策监管：政策、法规、国标、监管动态
- 行业观察：技术解读、评测体验、行业分析等一般内容
- 广告：明显的商业推广、带货、抽奖、软文
- 汽车无关：与汽车行业完全无关（生活、娱乐等）

china：布尔值，内容是否与中国汽车行业/中国市场/中国品牌相关。
中国车企出海、外企在华动态都算 true；纯海外品牌在海外市场的新闻、
单纯翻译转述外媒的内容为 false。

规则：label 拿不准时选「行业观察」；china 拿不准时选 true。
只有非常确定时才用「广告」「汽车无关」或 china=false，宁可放过不要误伤。
只输出 JSON：{"label": "<标签>", "china": true 或 false}"""


def parse_result(raw: str) -> Classification:
    """解析模型输出；任何异常回落到默认（可见、china=true）。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return Classification()
    if not isinstance(data, dict):
        return Classification()
    label = str(data.get("label", "")).strip()
    if label not in LABELS:
        label = DEFAULT_LABEL
    china = data.get("china")
    if not isinstance(china, bool):
        china = True
    return Classification(label=label, china=china)


def _post_text(post: Post) -> str:
    parts = [post.text_plain.strip()]
    if post.is_repost and post.retweeted_text_plain:
        parts.append(f"（转发自 @{post.retweeted_screen_name}）{post.retweeted_text_plain.strip()}")
    if post.video and post.video.title:
        parts.append(f"（视频：{post.video.title}）")
    return "\n".join(p for p in parts if p)[:2000]


async def classify_post(
    post: Post, settings: Settings, http_client: httpx.AsyncClient
) -> Classification:
    """给帖子分类。未启用/无 key/调用失败都返回默认值，不阻塞推送。"""
    if not settings.classification_enabled or not settings.deepseek_api_key:
        return Classification()
    text = _post_text(post)
    if not text:
        return Classification()

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
        return Classification()

    result = parse_result(raw)
    logger.info(
        "post classified: mid=%s label=%s china=%s", post.mid, result.label, result.china
    )
    return result
