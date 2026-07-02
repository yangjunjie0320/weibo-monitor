import httpx

from src.classifier import LABEL_CONTENT, classify_post, parse_label
from src.config import Settings
from tests.test_card import make_post


def test_parse_label_valid():
    assert parse_label('{"label": "观点"}') == "观点"
    assert parse_label('{"label": "广告"}') == "广告"


def test_parse_label_falls_back_to_content():
    # 宁可放过：任何异常输出都回落到「内容」
    assert parse_label("not json") == LABEL_CONTENT
    assert parse_label('{"label": "别的"}') == LABEL_CONTENT
    assert parse_label('{"other": 1}') == LABEL_CONTENT
    assert parse_label("[]") == LABEL_CONTENT


async def test_classify_disabled_returns_content():
    settings = Settings(classification_enabled=False, deepseek_api_key="sk-x")
    label = await classify_post(make_post(), settings, httpx.AsyncClient())
    assert label == LABEL_CONTENT


async def test_classify_without_key_returns_content():
    settings = Settings(classification_enabled=True, deepseek_api_key="")
    label = await classify_post(make_post(), settings, httpx.AsyncClient())
    assert label == LABEL_CONTENT


async def test_classify_api_error_returns_content(respx_mock):
    respx_mock.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(500)
    )
    settings = Settings(classification_enabled=True, deepseek_api_key="sk-x")
    async with httpx.AsyncClient() as client:
        label = await classify_post(make_post(), settings, client)
    assert label == LABEL_CONTENT


async def test_classify_success(respx_mock):
    respx_mock.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"label": "广告"}'}}]},
        )
    )
    settings = Settings(classification_enabled=True, deepseek_api_key="sk-x")
    async with httpx.AsyncClient() as client:
        label = await classify_post(make_post(), settings, client)
    assert label == "广告"
