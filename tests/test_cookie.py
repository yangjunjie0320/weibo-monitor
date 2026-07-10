import stat
import time

import httpx

from src.config import Settings
from src.cookie_refresh import build_cookie_string, cookie_is_stale, write_cookie_file
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
