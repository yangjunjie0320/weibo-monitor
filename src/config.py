from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WEIBO_MONITOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_id: str = ""
    app_secret: str = ""
    chat_id: str = ""

    accounts_file: str = "accounts.yaml"
    state_file: str = "state/seen.json"

    poll_interval_seconds: int = 600
    max_post_age_hours: int = 24
    max_pages_per_account: int = 3
    seen_mids_per_account: int = 200

    account_delay_min_seconds: float = 2.0
    account_delay_max_seconds: float = 4.0
    request_timeout: float = 20.0
    request_retries: int = 3
    send_retry_attempts: int = 3

    log_level: str = "INFO"
    log_dir: str = "logs"

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return cls(**data)
