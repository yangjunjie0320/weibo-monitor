import datetime as dt

from src.models import Account
from src.weibo import extract_mblogs, html_to_text, is_pinned, parse_post

ACCOUNT = Account(name="42号车库", uid="1644027280")


def test_extract_mblogs(timeline_data):
    pairs = extract_mblogs(timeline_data)
    assert len(pairs) == 12
    for card, mblog in pairs:
        assert card.get("card_type") == 9
        assert isinstance(mblog, dict)


def test_parse_post_fields(timeline_data):
    pairs = extract_mblogs(timeline_data)
    posts = [parse_post(ACCOUNT, card, mblog) for card, mblog in pairs]
    assert all(posts)
    for post in posts:
        assert post.uid == "1644027280"
        assert post.mid
        assert post.created_at.tzinfo is not None
        assert post.url.startswith("https://weibo.com/1644027280/")
    # fixture 已知包含 2 条置顶、2 条转发、8 条带图
    assert sum(1 for p in posts if p.is_pinned) == 2
    assert sum(1 for p in posts if p.is_repost) == 2
    assert sum(1 for p in posts if p.image_urls) == 8
    # 转发帖应带原帖文本
    repost = next(p for p in posts if p.is_repost)
    assert repost.retweeted_screen_name
    assert repost.retweeted_text_plain


def test_pinned_detection(timeline_data):
    pairs = extract_mblogs(timeline_data)
    pinned = [1 for card, mblog in pairs if is_pinned(card, mblog)]
    assert len(pinned) == 2


def test_parse_post_with_extend(timeline_data):
    card, mblog = next(
        (c, m) for c, m in extract_mblogs(timeline_data) if m.get("isLongText")
    )
    short = parse_post(ACCOUNT, card, mblog)
    extend = {"longTextContent": "完整的长文内容<br/>第二行"}
    full = parse_post(ACCOUNT, card, mblog, extend)
    assert full.text_plain == "完整的长文内容\n第二行"
    assert short.text_plain != full.text_plain


def test_html_to_text():
    assert html_to_text("a<br/>b") == "a\nb"
    assert html_to_text('看图<img alt="[笑cry]" src="x.png">') == "看图[笑cry]"
    assert html_to_text('<a href="/x">链接文字</a>&amp;') == "链接文字&"
    assert html_to_text(None) == ""


def test_parse_datetime_format():
    from src.weibo import parse_weibo_datetime

    parsed = parse_weibo_datetime("Wed Jun 25 10:30:00 +0800 2026")
    assert parsed == dt.datetime(2026, 6, 25, 10, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert parse_weibo_datetime("not a date") is None
    assert parse_weibo_datetime(None) is None
