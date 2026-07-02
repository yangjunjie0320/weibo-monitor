import httpx

from src.classifier import DEFAULT_LABEL, Classification, classify_post, parse_result
from src.config import Settings
from tests.test_card import make_post


def test_parse_result_valid():
    r = parse_result('{"label": "资本市场", "china": true}')
    assert r.label == "资本市场" and r.china is True
    r = parse_result('{"label": "汽车无关", "china": false}')
    assert r.label == "汽车无关" and r.china is False


def test_parse_result_falls_back():
    # 宁可放过：任何异常输出都回落到可见默认值
    assert parse_result("not json") == Classification()
    assert parse_result('{"label": "别的", "china": "maybe"}') == Classification()
    assert parse_result("[]") == Classification()
    assert parse_result('{"label": "广告"}').china is True


def test_should_drop_rules():
    settings = Settings(drop_offtopic=True, drop_ads=True, drop_non_china=True)
    assert Classification(label="汽车无关").should_drop(settings)
    assert Classification(label="广告").should_drop(settings)
    assert Classification(label="产品发布", china=False).should_drop(settings)
    assert not Classification(label="产品发布", china=True).should_drop(settings)

    lenient = Settings(drop_offtopic=False, drop_ads=False, drop_non_china=False)
    assert not Classification(label="汽车无关", china=False).should_drop(lenient)
    assert not Classification(label="广告").should_drop(lenient)


async def test_classify_disabled_returns_default():
    settings = Settings(classification_enabled=False, deepseek_api_key="sk-x")
    result = await classify_post(make_post(), settings, httpx.AsyncClient())
    assert result.label == DEFAULT_LABEL and result.china is True


async def test_classify_api_error_returns_default(respx_mock):
    respx_mock.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(500)
    )
    settings = Settings(classification_enabled=True, deepseek_api_key="sk-x")
    async with httpx.AsyncClient() as client:
        result = await classify_post(make_post(), settings, client)
    assert result == Classification()


async def test_classify_success(respx_mock):
    respx_mock.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"label": "市场数据", "china": true}'}}
                ]
            },
        )
    )
    settings = Settings(classification_enabled=True, deepseek_api_key="sk-x")
    async with httpx.AsyncClient() as client:
        result = await classify_post(make_post(), settings, client)
    assert result.label == "市场数据"
