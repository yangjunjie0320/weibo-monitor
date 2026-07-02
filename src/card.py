from __future__ import annotations

import json

from .models import Post

TEXT_FOLD_THRESHOLD = 500
TEXT_PREVIEW_CHARS = 300
REPOST_PREVIEW_CHARS = 300


def _fmt_count(value: int) -> str:
    if value >= 10000:
        return f"{value / 10000:.1f}万"
    return str(value)


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


def build_post_card(post: Post, image_key: str | None = None) -> str:
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
        elements.append(
            {
                "tag": "img",
                "img_key": image_key,
                "alt": {"tag": "plain_text", "content": ""},
            }
        )
    elif post.image_urls:
        note = f"图片 {len(post.image_urls)} 张（未能上传）"
        elements.append({"tag": "markdown", "content": note})

    stats = (
        f"转发 {_fmt_count(post.reposts_count)} · "
        f"评论 {_fmt_count(post.comments_count)} · "
        f"赞 {_fmt_count(post.attitudes_count)}"
    )
    elements.append({"tag": "markdown", "content": f"{stats}\n[打开原帖]({post.url})"})

    title = post.screen_name
    if post.is_repost:
        title += "（转发）"
    card = {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "body": {"elements": elements},
    }
    return json.dumps(card, ensure_ascii=False)
