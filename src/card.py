from __future__ import annotations

import json

from .classifier import DEFAULT_LABEL, FOLDED_LABELS
from .models import Post

TEXT_FOLD_THRESHOLD = 500
TEXT_PREVIEW_CHARS = 300
REPOST_PREVIEW_CHARS = 300

# 资本市场对海外读者价值最高，用醒目颜色区分
_TEMPLATES = {"资本市场": "orange", "谍照申报": "turquoise", "出海信息": "green"}


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


def _content_elements(
    post: Post, image_key: str | None, *, fold_images: bool = True
) -> list[dict[str, object]]:
    meta_parts = [post.created_at.strftime("%Y-%m-%d %H:%M")]
    if post.source:
        meta_parts.append(post.source)
    if post.region_name:
        meta_parts.append(post.region_name)
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
        img = {
            "tag": "img",
            "img_key": image_key,
            "alt": {"tag": "plain_text", "content": ""},
        }
        if fold_images:
            # 图片默认折叠；广告/无关卡片整体已在折叠面板里，不再嵌套
            total = len(post.image_urls)
            panel_title = f"**查看图片（共 {total} 张）**" if total > 1 else "**查看图片**"
            elements.append(
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {"title": {"tag": "markdown", "content": panel_title}},
                    "elements": [img],
                }
            )
        else:
            elements.append(img)
    elif post.image_urls:
        note = f"图片 {len(post.image_urls)} 张（未能上传）"
        elements.append({"tag": "markdown", "content": note})

    return elements


def build_post_card(post: Post, image_key: str | None = None, label: str = "") -> str:
    label = label or DEFAULT_LABEL
    folded = label in FOLDED_LABELS
    content = _content_elements(post, image_key, fold_images=not folded)

    if folded:
        elements: list[dict[str, object]] = [
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {"title": {"tag": "markdown", "content": f"**{label}** · 点开查看"}},
                "elements": content,
            }
        ]
    else:
        elements = content

    elements.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "打开原帖"},
            "type": "primary" if not folded else "default",
            "width": "default",
            "behaviors": [{"type": "open_url", "default_url": post.url}],
        }
    )

    title = post.screen_name
    if post.is_repost:
        title += "（转发）"
    title += f" · {label}"
    card = {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "grey" if folded else _TEMPLATES.get(label, "blue"),
        },
        "body": {"elements": elements},
    }
    return json.dumps(card, ensure_ascii=False)
