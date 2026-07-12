from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WEIBO_MONITOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        yaml_file=None,
        yaml_file_encoding="utf-8",
    )

    app_id: str = ""
    app_secret: str = ""
    chat_id: str = ""

    accounts_file: str = "accounts.yaml"
    state_file: str = "state/seen.json"
    health_file: str = "state/health.json"

    # 微博主数据源。official_cli 走开放平台批量接口；mobile 走 m.weibo.cn；
    # hybrid 以 mobile 为主、mobile 被限流期间自动切 official_cli 兜底。
    weibo_source: Literal["official_cli", "mobile", "hybrid"] = "official_cli"
    weibo_cli_path: str = ".tools/weibo-cli/node_modules/.bin/weibo"
    weibo_cli_version: str = "0.8.3"
    weibo_cli_timeout: float = Field(default=60.0, gt=0)
    weibo_cli_max_output_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    weibo_cli_max_users_per_batch: int = Field(default=20, ge=1, le=20)
    weibo_cli_count: int = Field(default=5, ge=1, le=20)
    legacy_extend_enabled: bool = True
    legacy_extend_state_file: str = "state/legacy-extend.json"
    legacy_extend_cooldown_seconds: int = Field(default=43200, gt=0)
    hybrid_state_file: str = "state/hybrid-source.json"
    hybrid_block_initial_seconds: int = Field(default=1800, gt=0)
    hybrid_block_max_seconds: int = Field(default=43200, gt=0)

    # 卡片转发按钮：点击转发到目标群，回调走长连接（需在开放平台把回调订阅
    # 方式设为长连接）。转发记录先落本地，再周期同步到多维表格归档。
    forward_enabled: bool = False
    forward_chat_id: str = ""  # 转发目标群（--list-chats 查）
    bitable_url: str = ""  # 归档多维表格分享链接（机器人需可编辑）
    bitable_table_name: str = "转发归档"
    bitable_sync_interval_seconds: int = Field(default=600, gt=0)
    card_store_file: str = "state/cards.json"
    forwarded_file: str = "state/forwarded.json"

    # 真实登录 cookie（可选）。填了就不用游客 cookie。
    # 字符串形如 "SUB=...; SUBP=...; ..."（浏览器登录 weibo.com 后从开发者工具复制）。
    weibo_cookie: str = ""
    weibo_cookie_file: str = "cookies/weibo.txt"  # 优先于 weibo_cookie

    # Playwright 持久 profile 自动刷新 cookie。先 `python main.py --browser-login`
    # 登录一次，之后 cookie 文件临近过期会从活体浏览器自动导出。
    cookie_refresh_enabled: bool = True
    browser_profile_dir: str = "browser-data/weibo"
    browser_headless: bool = True
    browser_timeout: float = Field(default=60.0, gt=0)
    cookie_stale_seconds: int = Field(default=86400 * 3, gt=0)
    cookie_refresh_min_interval: int = Field(default=3600, ge=0)

    # 限流使用长退避并持久化；普通上游故障连续出现时也提前结束本轮。
    rate_limit_rest_seconds: int = Field(default=1800, gt=0)
    rate_limit_rest_max_seconds: int = Field(default=43200, gt=0)
    rate_limit_jitter_ratio: float = Field(default=0.2, ge=0, le=1)
    upstream_failure_threshold: int = Field(default=3, gt=0)
    upstream_error_rest_seconds: int = Field(default=300, gt=0)

    poll_interval_seconds: int = Field(default=3600, gt=0)
    max_post_age_hours: int = Field(default=24, gt=0)
    max_pages_per_account: int = Field(default=3, gt=0)
    seen_mids_per_account: int = Field(default=200, gt=0)

    account_delay_min_seconds: float = Field(default=8.0, ge=0)
    account_delay_max_seconds: float = Field(default=15.0, ge=0)
    request_timeout: float = Field(default=20.0, gt=0)
    request_retries: int = Field(default=3, ge=0)
    send_retry_attempts: int = Field(default=3, gt=0)
    image_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)

    # DeepSeek 内容分类（车圈热点/产品发布/谍照申报/市场数据/资本市场/
    # 出海信息/政策监管/行业观察/广告/汽车无关）。广告、汽车无关和非中国内容
    # 默认不推送（分别可关闭）。
    classification_enabled: bool = True
    drop_offtopic: bool = True
    drop_ads: bool = True
    drop_non_china: bool = True
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    classify_timeout: float = Field(default=15.0, gt=0)

    log_level: str = "INFO"
    log_dir: str = "logs"
    console_log: bool = True

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Explicit values > environment > .env > YAML > defaults."""
        yaml_settings = YamlConfigSettingsSource(settings_cls)
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            yaml_settings,
            file_secret_settings,
        )

    @model_validator(mode="after")
    def validate_ranges_and_dependencies(self) -> Settings:
        if self.account_delay_min_seconds > self.account_delay_max_seconds:
            raise ValueError(
                "account_delay_min_seconds must not exceed "
                "account_delay_max_seconds"
            )
        if self.rate_limit_rest_seconds > self.rate_limit_rest_max_seconds:
            raise ValueError(
                "rate_limit_rest_max_seconds must be at least "
                "rate_limit_rest_seconds"
            )
        if self.forward_enabled and not self.forward_chat_id.strip():
            raise ValueError("forward_chat_id is required when forward_enabled is true")
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        yaml_path = Path(path)
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"cannot read YAML config {yaml_path}: {exc}") from exc
        if raw is not None and not isinstance(raw, dict):
            raise ValueError(f"YAML config root must be an object: {yaml_path}")

        class YamlSettings(cls):
            model_config = SettingsConfigDict(
                **{**cls.model_config, "yaml_file": yaml_path}
            )

        return YamlSettings()
