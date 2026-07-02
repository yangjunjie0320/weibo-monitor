from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import httpx
import lark_oapi as lark
import yaml

from src import log
from src.config import Settings
from src.models import Account
from src.monitor import Monitor
from src.sender import PostPusher
from src.state import StateStore
from src.weibo import WeiboClient


def load_accounts(path: str | Path) -> list[Account]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    accounts = [Account(**item) for item in data.get("accounts", [])]
    if not accounts:
        raise RuntimeError(f"no accounts configured in {path}")
    return accounts


def build_lark_client(settings: Settings) -> lark.Client:
    if not settings.app_id or not settings.app_secret:
        raise RuntimeError("app_id and app_secret must be configured")
    return (
        lark.Client.builder()
        .app_id(settings.app_id)
        .app_secret(settings.app_secret)
        .build()
    )


async def _run(settings: Settings, *, once: bool, dry_run: bool) -> None:
    logger = logging.getLogger(__name__)
    accounts = load_accounts(settings.accounts_file)

    lark_client = None
    if not dry_run:
        if not settings.chat_id:
            raise RuntimeError("chat_id must be configured (use --list-chats to pick one)")
        lark_client = build_lark_client(settings)

    async with httpx.AsyncClient() as http_client:
        weibo_client = WeiboClient(settings, http_client)
        await weibo_client.ensure_cookie()

        state = StateStore(settings.state_file, settings.seen_mids_per_account)
        pusher = PostPusher(settings, lark_client, http_client, dry_run=dry_run)
        monitor = Monitor(settings, weibo_client, state, pusher, accounts)

        logger.info(
            "weibo-monitor started: accounts=%d interval=%ds dry_run=%s",
            len(accounts),
            settings.poll_interval_seconds,
            dry_run,
        )
        if once:
            await monitor.run_cycle()
        else:
            await monitor.run_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Weibo monitor: push new posts to Feishu")
    parser.add_argument("--config", metavar="PATH", help="YAML config file path")
    parser.add_argument("--list-chats", action="store_true", help="list chats the bot is in")
    parser.add_argument(
        "--browser-login",
        action="store_true",
        help="open a headed browser to log in to weibo once (seeds the cookie profile)",
    )
    parser.add_argument("--once", action="store_true", help="run a single poll cycle and exit")
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch and diff but log instead of sending"
    )
    args = parser.parse_args()

    config_path = args.config or ("config.yaml" if Path("config.yaml").exists() else None)
    settings = Settings.from_yaml(config_path) if config_path else Settings()

    log.setup(level=settings.log_level, log_dir=settings.log_dir)

    if args.list_chats:
        from src.chats import list_chats

        for chat_id, name in list_chats(build_lark_client(settings)):
            print(f"{chat_id}\t{name}")
        return

    if args.browser_login:
        from src.cookie_refresh import browser_login

        ok = asyncio.run(browser_login(settings))
        sys.exit(0 if ok else 1)

    try:
        asyncio.run(_run(settings, once=args.once, dry_run=args.dry_run))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.getLogger(__name__).critical("fatal: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
