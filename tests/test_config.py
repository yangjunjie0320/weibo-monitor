import pytest
from pydantic import ValidationError

from src.config import Settings


def test_environment_and_dotenv_override_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "poll_interval_seconds: 111\nforward_enabled: false\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "WEIBO_MONITOR_POLL_INTERVAL_SECONDS=222\n", encoding="utf-8"
    )
    monkeypatch.setenv("WEIBO_MONITOR_POLL_INTERVAL_SECONDS", "333")

    assert Settings.from_yaml(yaml_path).poll_interval_seconds == 333
    monkeypatch.delenv("WEIBO_MONITOR_POLL_INTERVAL_SECONDS")
    assert Settings.from_yaml(yaml_path).poll_interval_seconds == 222
    (tmp_path / ".env").unlink()
    assert Settings.from_yaml(yaml_path).poll_interval_seconds == 111


def test_yaml_unknown_key_is_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text("typo_poll_interval: 10\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="typo_poll_interval"):
        Settings.from_yaml(path)


def test_non_mapping_yaml_is_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="root must be an object"):
        Settings.from_yaml(path)


def test_forward_target_is_required_only_when_enabled():
    assert not Settings().forward_enabled
    with pytest.raises(ValidationError, match="forward_chat_id"):
        Settings(forward_enabled=True)
    assert Settings(forward_enabled=True, forward_chat_id="oc_target").forward_enabled


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"request_timeout": 0}, "request_timeout"),
        (
            {"account_delay_min_seconds": 2, "account_delay_max_seconds": 1},
            "account_delay_min_seconds",
        ),
        (
            {"rate_limit_rest_seconds": 60, "rate_limit_rest_max_seconds": 30},
            "rate_limit_rest_max_seconds",
        ),
        ({"rate_limit_jitter_ratio": 1.1}, "rate_limit_jitter_ratio"),
        ({"image_max_bytes": 0}, "image_max_bytes"),
    ],
)
def test_settings_reject_invalid_ranges(overrides, message):
    with pytest.raises(ValidationError, match=message):
        Settings(**overrides)


def test_reliability_defaults():
    settings = Settings()
    assert settings.health_file == "state/health.json"
    assert settings.rate_limit_rest_seconds == 1800
    assert settings.rate_limit_rest_max_seconds == 43200
    assert settings.rate_limit_jitter_ratio == 0.2
    assert settings.upstream_failure_threshold == 3
    assert settings.image_max_bytes == 10 * 1024 * 1024
    assert settings.console_log
