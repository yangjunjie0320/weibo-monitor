import datetime as dt
import json

from src.card import build_post_card
from src.models import Post, VideoInfo

CST = dt.timezone(dt.timedelta(hours=8))


def make_post(**overrides) -> Post:
    base = {
        "uid": "42",
        "screen_name": "测试博主",
        "mid": "m1",
        "bid": "Babc",
        "created_at": dt.datetime(2026, 7, 1, 12, 30, tzinfo=CST),
        "text_plain": "今天试驾了一台新车",
        "source": "微博网页版",
        "reposts_count": 12,
        "comments_count": 34,
        "attitudes_count": 56789,
    }
    base.update(overrides)
    return Post(**base)


def find_rating_row(card: dict) -> dict:
    return next(el for el in card["body"]["elements"] if el["tag"] == "column_set")


def test_title_is_label_author_in_meta():
    card = build_post_card(make_post(), label="市场数据")
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "市场数据"
    assert card["header"]["template"] == "wathet"
    meta = card["body"]["elements"][0]["content"]
    assert "测试博主" in meta and "07-01 12:30" in meta
    # 原帖入口是元信息行里的超链接，不再是按钮
    assert "[原帖](https://weibo.com/42/Babc)" in meta
    dumped = json.dumps(card, ensure_ascii=False)
    assert "今天试驾了一台新车" in dumped
    # 互动数、发送方式、地理位置都不展示
    assert "转发 12" not in dumped
    assert "56789" not in dumped and "5.7万" not in dumped
    assert "微博网页版" not in dumped
    assert not any(el["tag"] == "button" for el in card["body"]["elements"])


def test_rating_row_and_mark_rated():
    from src.card import mark_rated

    card = build_post_card(make_post(), label="市场数据")
    row = find_rating_row(card)
    buttons = [col["elements"][0] for col in row["columns"]]
    assert [b["text"]["content"] for b in buttons] == ["三分", "两分", "一分"]
    values = [b["behaviors"][0]["value"] for b in buttons]
    assert [v["score"] for v in values] == [3, 2, 1]
    assert all(v["action"] == "rate" and v["mid"] == "m1" for v in values)

    rated = mark_rated(card, 3)
    assert not any(el["tag"] == "column_set" for el in rated["body"]["elements"])
    assert any(
        el["tag"] == "markdown" and "已打分：3 分" in el["content"]
        for el in rated["body"]["elements"]
    )
    # mark_rated 不改原卡片（patch 失败还要留原样）
    assert any(el["tag"] == "column_set" for el in card["body"]["elements"])


def test_no_rating_row_when_disabled():
    card = build_post_card(make_post(), with_rating=False)
    assert not any(el["tag"] == "column_set" for el in card["body"]["elements"])


def test_default_label():
    card = build_post_card(make_post())
    assert card["header"]["title"]["content"] == "行业观察"
    assert card["header"]["template"] == "indigo"


def test_each_category_has_unique_color():
    from src.card import _TEMPLATES
    from src.classifier import CATEGORIES

    colors = [_TEMPLATES[c] for c in CATEGORIES]
    assert len(set(colors)) == len(CATEGORIES)


def test_long_text_folds():
    card = build_post_card(make_post(text_plain="长" * 600))
    tags = [el["tag"] for el in card["body"]["elements"]]
    assert "collapsible_panel" in tags


def test_repost_marker_and_quote():
    post = make_post(
        is_repost=True,
        retweeted_screen_name="原作者",
        retweeted_text_plain="原帖内容",
    )
    card = build_post_card(post, label="车圈热点")
    assert card["header"]["title"]["content"] == "车圈热点"
    meta = card["body"]["elements"][0]["content"]
    assert "转发" in meta
    dumped = json.dumps(card, ensure_ascii=False)
    assert "@原作者" in dumped
    assert "原帖内容" in dumped


def test_image_folded_as_markdown():
    post = make_post(
        image_urls=["https://wx1.sinaimg.cn/large/x.jpg", "https://wx1.sinaimg.cn/y.jpg"],
        video=VideoInfo(title="试驾视频", duration=95),
    )
    with_key = build_post_card(post, image_key="img_v3_xxx")
    top_tags = [el["tag"] for el in with_key["body"]["elements"]]
    assert "img" not in top_tags  # img 组件在折叠面板里渲染不稳定，必须走 markdown
    panel = next(
        el
        for el in with_key["body"]["elements"]
        if el["tag"] == "collapsible_panel" and "查看图片" in el["header"]["title"]["content"]
    )
    assert panel["expanded"] is False
    assert "共 2 张" in panel["header"]["title"]["content"]
    assert panel["elements"][0] == {"tag": "markdown", "content": "![图片](img_v3_xxx)"}
    assert "1:35" in json.dumps(with_key, ensure_ascii=False)

    without_key = build_post_card(post)
    assert "未能上传" in json.dumps(without_key, ensure_ascii=False)
