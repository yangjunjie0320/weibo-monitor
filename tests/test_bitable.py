import pytest

from src.bitable import BitableError, BitableSyncer, _parse_url_token, _to_ms
from src.config import Settings
from src.forward import ForwardStore


def test_parse_url_token():
    assert _parse_url_token("https://x.feishu.cn/base/AbC123?table=t") == ("base", "AbC123")
    assert _parse_url_token(
        "https://skyland2020.feishu.cn/wiki/DydSwixm1i2uRTkXIFucEDMAnkc?from=from_copylink"
    ) == ("wiki", "DydSwixm1i2uRTkXIFucEDMAnkc")
    with pytest.raises(BitableError):
        _parse_url_token("https://x.feishu.cn/docs/xxx")


def test_to_ms():
    assert _to_ms("2026-07-01T12:30:00+08:00") == 1782880200000
    assert _to_ms("") is None
    assert _to_ms("not-a-date") is None


def test_to_fields(tmp_path):
    syncer = BitableSyncer(Settings(), None, ForwardStore(tmp_path / "f.json"))
    rec = {
        "screen_name": "测试博主",
        "label": "市场数据",
        "summary": "今天试驾了一台新车",
        "url": "https://weibo.com/42/Babc",
        "forwarder_open_id": "ou_laoma",
        "post_created_at": "2026-07-01T12:30:00+08:00",
        "forwarded_at": "2026-07-01T05:00:00+00:00",
        "synced": False,
    }
    fields = syncer._to_fields("m1", rec)
    assert fields["博主"] == "测试博主"
    assert fields["原帖链接"] == {"text": "原帖", "link": "https://weibo.com/42/Babc"}
    assert fields["转发人"] == "ou_laoma"
    assert isinstance(fields["发帖时间"], int) and isinstance(fields["转发时间"], int)
    assert fields["mid"] == "m1"
