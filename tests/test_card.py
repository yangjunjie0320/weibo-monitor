import datetime as dt
import json

from src.card import build_post_card
from src.classifier import LABEL_AD, LABEL_OFFTOPIC, LABEL_OPINION
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


def find_button(card: dict) -> dict:
    return next(el for el in card["body"]["elements"] if el["tag"] == "button")


def test_basic_card():
    card = json.loads(build_post_card(make_post(), label=LABEL_OPINION))
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "测试博主 · 观点"
    dumped = json.dumps(card, ensure_ascii=False)
    assert "今天试驾了一台新车" in dumped
    assert "2026-07-01 12:30" in dumped
    # 互动数不再展示
    assert "转发 12" not in dumped
    assert "56789" not in dumped and "5.7万" not in dumped
    # 打开原帖是按钮
    button = find_button(card)
    assert button["behaviors"][0]["default_url"] == "https://weibo.com/42/Babc"


def test_default_label_is_content():
    card = json.loads(build_post_card(make_post()))
    assert card["header"]["title"]["content"] == "测试博主 · 内容"
    assert card["header"]["template"] == "blue"


def test_ad_and_offtopic_fold():
    for label in (LABEL_AD, LABEL_OFFTOPIC):
        card = json.loads(build_post_card(make_post(), label=label))
        assert card["header"]["template"] == "grey"
        top_tags = [el["tag"] for el in card["body"]["elements"]]
        # 正文整体收进折叠面板，按钮留在外面
        assert top_tags == ["collapsible_panel", "button"]
        panel = card["body"]["elements"][0]
        assert panel["expanded"] is False
        assert label in panel["header"]["title"]["content"]


def test_long_text_folds():
    card = json.loads(build_post_card(make_post(text_plain="长" * 600)))
    tags = [el["tag"] for el in card["body"]["elements"]]
    assert "collapsible_panel" in tags


def test_repost_card():
    post = make_post(
        is_repost=True,
        retweeted_screen_name="原作者",
        retweeted_text_plain="原帖内容",
    )
    card = json.loads(build_post_card(post))
    assert card["header"]["title"]["content"] == "测试博主（转发） · 内容"
    dumped = json.dumps(card, ensure_ascii=False)
    assert "@原作者" in dumped
    assert "原帖内容" in dumped


def test_image_and_video():
    post = make_post(
        image_urls=["https://wx1.sinaimg.cn/large/x.jpg"],
        video=VideoInfo(title="试驾视频", duration=95),
    )
    with_key = json.loads(build_post_card(post, image_key="img_v3_xxx"))
    tags = [el["tag"] for el in with_key["body"]["elements"]]
    assert "img" in tags
    assert "1:35" in json.dumps(with_key, ensure_ascii=False)

    without_key = json.loads(build_post_card(post))
    assert "未能上传" in json.dumps(without_key, ensure_ascii=False)
