from __future__ import annotations

import json

from .classifier import DEFAULT_LABEL
from .models import Post

TEXT_FOLD_THRESHOLD = 500
TEXT_PREVIEW_CHARS = 300
REPOST_PREVIEW_CHARS = 300

# 每个分类一个专属颜色（飞书卡片 header template）
_TEMPLATES = {
    "车圈热点": "red",
    "产品发布": "blue",
    "谍照申报": "turquoise",
    "市场数据": "wathet",
    "资本市场": "orange",
    "出海信息": "green",
    "政策监管": "violet",
    "行业观察": "indigo",
}


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    total = int(seconds)
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def build_post_card(post: Post, image_key: str | None = None, label: str = "") -> str:
    label = label or DEFAULT_LABEL

    # 标题是分类；作者、时间（和转发标记）放第一行
    meta_parts = [f"**{post.screen_name}**", post.created_at.strftime("%m-%d %H:%M")]
    if post.is_repost:
        meta_parts.append("转发")
    elements: list[dict[str, object]] = [
        {"tag": "markdown", "content": " · ".join(meta_parts)}
    ]

    text = post.text_plain.strip()
    if text and len(text) > TEXT_FOLD_THRESHOLD:
        elements.append({"tag": "markdown", "content": _truncate(text, TEXT_PREVIEW_CHARS)})
        elements.append(
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {"title": {"tag": "markdown", "content": "**展开全文**"}},
                "elements": [{"tag": "markdown", "content": text}],
            }
        )
    elif text:
        elements.append({"tag": "markdown", "content": text})

    if post.is_repost:
        quoted = _truncate(post.retweeted_text_plain, REPOST_PREVIEW_CHARS)
        author = f"@{post.retweeted_screen_name}" if post.retweeted_screen_name else "原帖"
        lines = "\n".join(f"> {line}" for line in f"转发自 {author}：\n{quoted}".splitlines())
        elements.append({"tag": "markdown", "content": lines})

    if post.video:
        video_line = "视频"
        if post.video.title:
            video_line += f"：{post.video.title}"
        duration = _fmt_duration(post.video.duration)
        if duration:
            video_line += f"（{duration}）"
        elements.append({"tag": "markdown", "content": video_line})

    if image_key:
        # 折叠面板内必须用 markdown 图片语法：img 组件在面板里渲染时有时无
        total = len(post.image_urls)
        panel_title = f"**查看图片（共 {total} 张）**" if total > 1 else "**查看图片**"
        elements.append(
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {"title": {"tag": "markdown", "content": panel_title}},
                "elements": [
                    {"tag": "markdown", "content": f"![图片]({image_key})"}
                ],
            }
        )
    elif post.image_urls:
        note = f"图片 {len(post.image_urls)} 张（未能上传）"
        elements.append({"tag": "markdown", "content": note})

    elements.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开原帖"},
            "type": "primary",
            "width": "default",
            "behaviors": [{"type": "open_url", "default_url": post.url}],
        }
    )

    card = {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": label},
            "template": _TEMPLATES.get(label, "blue"),
        },
        "body": {"elements": elements},
    }
    return json.dumps(card, ensure_ascii=False)
