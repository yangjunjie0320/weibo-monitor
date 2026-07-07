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
from src.bitable import BitableSyncer
from src.card_store import CardStore
from src.config import Settings
from src.forward import ForwardService, ForwardStore
from src.listener import CardActionListener
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


async def _consume_forwards(listener: CardActionListener, service: ForwardService) -> None:
    logger = logging.getLogger(__name__)
    async for event in listener.listen():
        try:
            await service.process(event)
        except Exception:
            logger.exception("forward process failed: mid=%s", event.mid)


def _log_task_death(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logging.getLogger(__name__).critical(
            "background task %s died: %s", task.get_name(), exc
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

        forward_on = settings.forward_enabled and lark_client is not None
        card_store = CardStore(settings.card_store_file) if forward_on else None

        state = StateStore(settings.state_file, settings.seen_mids_per_account)
        pusher = PostPusher(
            settings, lark_client, http_client, card_store=card_store, dry_run=dry_run
        )
        monitor = Monitor(settings, weibo_client, state, pusher, accounts)

        logger.info(
            "weibo-monitor started: accounts=%d interval=%ds dry_run=%s forward=%s",
            len(accounts),
            settings.poll_interval_seconds,
            dry_run,
            forward_on,
        )
        if once:
            await monitor.run_cycle()
            return

        # 转发侧任务（长连接监听 + 多维表格归档同步）与轮询并行；
        # 它们意外挂掉只记日志，不拖垮监控主循环
        side_tasks: list[asyncio.Task] = []
        if forward_on:
            forward_store = ForwardStore(settings.forwarded_file)
            service = ForwardService(settings, lark_client, card_store, forward_store)
            listener = CardActionListener(settings, service.accept)
            side_tasks.append(
                asyncio.create_task(_consume_forwards(listener, service), name="forward-listener")
            )
            if settings.bitable_url:
                syncer = BitableSyncer(settings, lark_client, forward_store)
                side_tasks.append(
                    asyncio.create_task(syncer.run_forever(), name="bitable-sync")
                )
            else:
                logger.warning("forward enabled but bitable_url not set, archive stays local")
        for task in side_tasks:
            task.add_done_callback(_log_task_death)

        try:
            await monitor.run_forever()
        finally:
            for task in side_tasks:
                task.cancel()


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
