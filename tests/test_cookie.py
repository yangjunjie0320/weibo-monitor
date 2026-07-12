import stat
import time
from contextlib import asynccontextmanager

import httpx

from src import cookie_refresh
from src.config import Settings
from src.cookie_refresh import (
    build_cookie_string,
    cookie_is_stale,
    refresh_weibo_cookie,
    write_cookie_file,
)
from src.weibo import WeiboClient


def test_cookie_string_roundtrip(tmp_path):
    cookies = [
        {"name": "SUB", "value": "abc", "domain": ".weibo.cn"},
        {"name": "SUBP", "value": "def", "domain": ".weibo.cn"},
    ]
    target = tmp_path / "weibo.txt"
    write_cookie_file(cookies, str(target))
    assert target.read_text(encoding="utf-8").strip() == "SUB=abc; SUBP=def"
    assert build_cookie_string(cookies) == "SUB=abc; SUBP=def"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_cookie_staleness(tmp_path):
    target = tmp_path / "weibo.txt"
    assert cookie_is_stale(str(target), 60)  # 不存在算过期
    target.write_text("SUB=abc", encoding="utf-8")
    assert not cookie_is_stale(str(target), 60)
    old = time.time() - 120
    import os

    os.utime(target, (old, old))
    assert cookie_is_stale(str(target), 60)


def test_client_loads_static_cookie_from_file(tmp_path):
    cookie_file = tmp_path / "weibo.txt"
    cookie_file.write_text("SUB=abc; SUBP=def\n", encoding="utf-8")
    settings = Settings(weibo_cookie_file=str(cookie_file))
    client = WeiboClient(settings, httpx.AsyncClient())
    assert client.uses_static_cookie

    # 文件更新后 reload 生效
    cookie_file.write_text("SUB=new\n", encoding="utf-8")
    client.reload_static_cookie()
    assert client._cookie == "SUB=new"


def test_client_falls_back_to_visitor_without_cookie(tmp_path):
    settings = Settings(weibo_cookie_file=str(tmp_path / "missing.txt"), weibo_cookie="")
    client = WeiboClient(settings, httpx.AsyncClient())
    assert not client.uses_static_cookie


async def test_refresh_recovers_via_sso_when_mobile_session_expired(tmp_path, monkeypatch):
    """m.weibo.cn 会话过期但主站会话还在：访问主站触发 SSO 后应刷新成功。"""
    cookie_file = tmp_path / "weibo.txt"
    settings = Settings(weibo_cookie_file=str(cookie_file))
    login_states = iter([False, True])
    sso_visits: list[str] = []

    @asynccontextmanager
    async def fake_context(*args, **kwargs):
        yield object()

    async def fake_is_logged_in(context):
        return next(login_states)

    async def fake_trigger_sso(context, timeout_ms):
        sso_visits.append("weibo.com")

    async def fake_export(context, timeout_ms):
        return [{"name": "SUB", "value": "fresh", "domain": ".weibo.cn"}]

    monkeypatch.setattr(cookie_refresh, "persistent_context", fake_context)
    monkeypatch.setattr(cookie_refresh, "_is_logged_in", fake_is_logged_in)
    monkeypatch.setattr(cookie_refresh, "_trigger_sso", fake_trigger_sso)
    monkeypatch.setattr(cookie_refresh, "_export_cookies", fake_export)

    assert await refresh_weibo_cookie(settings)
    assert sso_visits == ["weibo.com"]
    assert cookie_file.read_text(encoding="utf-8").strip() == "SUB=fresh"


async def test_refresh_fails_when_sso_does_not_recover(tmp_path, monkeypatch):
    settings = Settings(weibo_cookie_file=str(tmp_path / "weibo.txt"))

    @asynccontextmanager
    async def fake_context(*args, **kwargs):
        yield object()

    async def fake_is_logged_in(context):
        return False

    async def fake_trigger_sso(context, timeout_ms):
        pass

    monkeypatch.setattr(cookie_refresh, "persistent_context", fake_context)
    monkeypatch.setattr(cookie_refresh, "_is_logged_in", fake_is_logged_in)
    monkeypatch.setattr(cookie_refresh, "_trigger_sso", fake_trigger_sso)

    assert not await refresh_weibo_cookie(settings)
    assert not (tmp_path / "weibo.txt").exists()
