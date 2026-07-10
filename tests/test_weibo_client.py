from unittest.mock import AsyncMock

import httpx
import pytest

from src.config import Settings
from src.weibo import RateLimitedError, UpstreamError, WeiboClient


def settings(tmp_path, **overrides) -> Settings:
    values = {
        "weibo_cookie_file": str(tmp_path / "missing-cookie.txt"),
        "cookie_refresh_enabled": False,
        "request_retries": 0,
        "forward_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize("status_code", [403, 418, 429, 432])
async def test_rate_limit_statuses_do_not_retry(tmp_path, status_code):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, content=b"")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WeiboClient(settings(tmp_path, request_retries=3), http)
        with pytest.raises(RateLimitedError) as caught:
            await client.timeline_page("42", 1)

    assert calls == 1
    assert caught.value.status_code == status_code


async def test_retry_after_is_exposed(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WeiboClient(settings(tmp_path), http)
        with pytest.raises(RateLimitedError) as caught:
            await client.timeline_page("42", 1)

    assert caught.value.retry_after_seconds == 120


async def test_5xx_retries_then_succeeds(tmp_path, monkeypatch):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": 1, "data": {"cards": []}})

    monkeypatch.setattr("src.weibo.asyncio.sleep", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WeiboClient(settings(tmp_path, request_retries=1), http)
        result = await client.timeline_page("42", 1)

    assert result["ok"] == 1
    assert calls == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"ok": 0, "msg": "unknown"}),
        httpx.Response(200, content=b""),
    ],
)
async def test_invalid_success_response_is_upstream_error(tmp_path, response):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    ) as http:
        client = WeiboClient(settings(tmp_path), http)
        with pytest.raises(UpstreamError):
            await client.timeline_page("42", 1)


async def test_captcha_json_is_rate_limited(tmp_path):
    response = httpx.Response(
        200,
        json={"ok": -100, "url": "https://m.weibo.cn/captcha"},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    ) as http:
        client = WeiboClient(settings(tmp_path), http)
        with pytest.raises(RateLimitedError):
            await client.timeline_page("42", 1)


async def test_401_refreshes_auth_once(tmp_path, monkeypatch):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": 1, "data": {"cards": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WeiboClient(settings(tmp_path), http)
        refresh = AsyncMock(return_value=True)
        monkeypatch.setattr(client, "_refresh_auth_cookie", refresh)
        result = await client.timeline_page("42", 1)

    assert result["ok"] == 1
    refresh.assert_awaited_once()
    assert calls == 2


async def test_visitor_endpoint_rate_limit_is_not_parsed_or_retried(tmp_path):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(432, content=b"sensitive challenge body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WeiboClient(settings(tmp_path, request_retries=3), http)
        with pytest.raises(RateLimitedError) as caught:
            await client.refresh_visitor_cookie()

    assert calls == 1
    assert caught.value.status_code == 432


async def test_invalid_visitor_body_is_upstream_error(tmp_path):
    response = httpx.Response(200, content=b"not a callback")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    ) as http:
        client = WeiboClient(settings(tmp_path), http)
        with pytest.raises(UpstreamError, match="invalid visitor response"):
            await client.refresh_visitor_cookie()
