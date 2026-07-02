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

    # 真实登录 cookie（可选）。填了就不用游客 cookie。
    # 字符串形如 "SUB=...; SUBP=...; ..."（浏览器登录 weibo.com 后从开发者工具复制）。
    weibo_cookie: str = ""
    weibo_cookie_file: str = "cookies/weibo.txt"  # 优先于 weibo_cookie

    # Playwright 持久 profile 自动刷新 cookie。先 `python main.py --browser-login`
    # 登录一次，之后 cookie 文件临近过期会从活体浏览器自动导出。
    cookie_refresh_enabled: bool = True
    browser_profile_dir: str = "browser-data/weibo"
    browser_headless: bool = True
    browser_timeout: float = 60.0
    cookie_stale_seconds: int = 86400 * 3      # 文件超过 3 天算过期
    cookie_refresh_min_interval: int = 3600    # 进程内刷新节流

    # 触发限流（captcha 挑战）后本轮熔断，至少休息这么久再开下一轮
    rate_limit_rest_seconds: int = 900

    poll_interval_seconds: int = 600
    max_post_age_hours: int = 24
    max_pages_per_account: int = 3
    seen_mids_per_account: int = 200

    account_delay_min_seconds: float = 2.0
    account_delay_max_seconds: float = 4.0
    request_timeout: float = 20.0
    request_retries: int = 3
    send_retry_attempts: int = 3

    # DeepSeek 内容分类（车圈热点/产品发布/谍照申报/市场数据/资本市场/
    # 出海信息/政策监管/行业观察/广告/汽车无关）。广告折叠展示；
    # 汽车无关和非中国内容直接不推送（可关）。
    classification_enabled: bool = True
    drop_offtopic: bool = True    # 汽车无关的帖子不推送
    drop_non_china: bool = True   # 与中国汽车行业无关的内容不推送
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    classify_timeout: float = 15.0

    log_level: str = "INFO"
    log_dir: str = "logs"

    @classmethod
    def from_yaml(cls, path: str | Path) -> Settings:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return cls(**data)
