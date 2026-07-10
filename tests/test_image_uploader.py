import httpx
import pytest

from src.image_uploader import ImageFetchError, _download_image, _safe_url_for_log


async def test_download_image_allows_sinaimg_https_and_relative_redirect():
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "wx1.sinaimg.cn":
            return httpx.Response(302, headers={"Location": "//wx2.sinaimg.cn/final.jpg"})
        return httpx.Response(
            200,
            headers={"Content-Type": "image/jpeg", "Content-Length": "3"},
            content=b"img",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _download_image(
            "https://wx1.sinaimg.cn/start.jpg", client, max_bytes=10
        )

    assert result == b"img"
    assert requested == [
        "https://wx1.sinaimg.cn/start.jpg",
        "https://wx2.sinaimg.cn/final.jpg",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://wx1.sinaimg.cn/a.jpg",
        "https://example.com/a.jpg",
        "https://sinaimg.cn.example.com/a.jpg",
        "https://user:password@wx1.sinaimg.cn/a.jpg",
        "https://wx1.sinaimg.cn:8443/a.jpg",
    ],
)
async def test_download_image_rejects_untrusted_initial_url(url):
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=b"x")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ImageFetchError):
            await _download_image(url, client)
    assert requests == 0


async def test_download_image_rejects_redirect_to_untrusted_host():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://example.com/secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ImageFetchError, match=r"sinaimg\.cn"):
            await _download_image("https://wx1.sinaimg.cn/a.jpg", client)


async def test_download_image_rejects_too_many_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        hop = int(request.url.params.get("hop", "0"))
        return httpx.Response(
            302, headers={"Location": f"https://wx1.sinaimg.cn/a.jpg?hop={hop + 1}"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ImageFetchError, match="3 redirects"):
            await _download_image("https://wx1.sinaimg.cn/a.jpg", client)


@pytest.mark.parametrize("content_type", ["text/html", "", "application/json"])
async def test_download_image_requires_image_content_type(content_type):
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Content-Type": content_type} if content_type else {}
        return httpx.Response(200, headers=headers, content=b"not-image")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ImageFetchError, match="Content-Type"):
            await _download_image("https://wx1.sinaimg.cn/a.jpg", client)


async def test_download_image_enforces_declared_and_streamed_size():
    responses = iter(
        [
            httpx.Response(
                200,
                headers={"Content-Type": "image/jpeg", "Content-Length": "11"},
                content=b"x",
            ),
            httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=b"x" * 11),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ImageFetchError, match="Content-Length"):
            await _download_image("https://wx1.sinaimg.cn/a.jpg", client, max_bytes=10)
        with pytest.raises(ImageFetchError, match="exceeds"):
            await _download_image("https://wx1.sinaimg.cn/a.jpg", client, max_bytes=10)


def test_safe_log_url_removes_credentials_and_query():
    assert (
        _safe_url_for_log("https://user:secret@wx1.sinaimg.cn/a.jpg?token=secret")
        == "https://wx1.sinaimg.cn/a.jpg"
    )
