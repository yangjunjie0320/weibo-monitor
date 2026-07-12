from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx
import lark_oapi as lark
import yaml

from src import log
from src.atomic_json import load_json_object
from src.bitable import BitableSyncer
from src.card_store import CardStore
from src.config import Settings
from src.forward import ForwardService, ForwardStore
from src.health import HealthStore
from src.listener import CardActionListener
from src.models import Account
from src.monitor import Monitor
from src.sender import PostPusher
from src.state import StateStore
from src.weibo import RateLimitedError, WeiboClient, extract_mblogs
from src.weibo_cli import OfficialCliClient, check_cli_install
from src.weibo_hybrid import HybridClient


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


async def _run_supervised(monitor: Monitor, side_tasks: list[asyncio.Task]) -> None:
    """Run all essential service tasks; any unexpected exit terminates the process."""

    monitor_task = asyncio.create_task(monitor.run_forever(), name="monitor")
    tasks = [monitor_task, *side_tasks]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                raise RuntimeError(f"essential task cancelled: {task.get_name()}")
            exc = task.exception()
            if exc is not None:
                raise RuntimeError(f"essential task died: {task.get_name()}: {exc}") from exc
            raise RuntimeError(f"essential task exited unexpectedly: {task.get_name()}")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run(settings: Settings, *, once: bool, dry_run: bool) -> None:
    logger = logging.getLogger(__name__)
    accounts = load_accounts(settings.accounts_file)

    lark_client = None
    if not dry_run:
        if not settings.chat_id:
            raise RuntimeError("chat_id must be configured (use --list-chats to pick one)")
        lark_client = build_lark_client(settings)

    async with httpx.AsyncClient() as http_client:
        mobile_client = WeiboClient(settings, http_client)
        if settings.weibo_source == "official_cli":
            weibo_client = OfficialCliClient(
                settings,
                legacy_client=mobile_client if settings.legacy_extend_enabled else None,
                read_only=dry_run,
            )
        elif settings.weibo_source == "hybrid":
            cli_client = OfficialCliClient(
                settings,
                legacy_client=mobile_client if settings.legacy_extend_enabled else None,
                read_only=dry_run,
            )
            weibo_client = HybridClient(
                settings, mobile_client, cli_client, read_only=dry_run
            )
            await weibo_client.ensure_cookie()
        else:
            weibo_client = mobile_client
            await weibo_client.ensure_cookie()

        forward_on = settings.forward_enabled and lark_client is not None
        card_store = CardStore(settings.card_store_file) if forward_on else None

        state = StateStore(
            settings.state_file,
            settings.seen_mids_per_account,
            read_only=dry_run,
        )
        health = HealthStore(settings.health_file, read_only=dry_run)
        pusher = PostPusher(
            settings, lark_client, http_client, card_store=card_store, dry_run=dry_run
        )
        monitor = Monitor(settings, weibo_client, state, pusher, accounts, health)

        logger.info(
            "weibo-monitor started: accounts=%d interval=%ds dry_run=%s forward=%s",
            len(accounts),
            settings.poll_interval_seconds,
            dry_run,
            forward_on,
        )
        if once:
            summary = await monitor.run_cycle()
            monitor.finish_once(summary)
            return

        # 转发侧任务（长连接、重试队列、多维表格归档）与轮询并行；
        # 任一关键任务意外退出都会让进程失败，由 launchd 拉起全套服务。
        side_tasks: list[asyncio.Task] = []
        if forward_on:
            forward_store = ForwardStore(settings.forwarded_file)
            service = ForwardService(settings, lark_client, card_store, forward_store)
            listener = CardActionListener(settings, service.accept)
            side_tasks.append(
                asyncio.create_task(_consume_forwards(listener, service), name="forward-listener")
            )
            side_tasks.append(
                asyncio.create_task(service.run_forever(), name="forward-retry")
            )
            if settings.bitable_url:
                syncer = BitableSyncer(settings, lark_client, forward_store)
                side_tasks.append(
                    asyncio.create_task(syncer.run_forever(), name="bitable-sync")
                )
            else:
                logger.warning("forward enabled but bitable_url not set, archive stays local")
        await _run_supervised(monitor, side_tasks)


def _self_check(settings: Settings, config_path: str | Path | None = None) -> None:
    accounts = load_accounts(settings.accounts_file)
    if not settings.app_id or not settings.app_secret or not settings.chat_id:
        raise RuntimeError("app_id, app_secret and chat_id must be configured")

    for raw_path in (
        settings.state_file,
        settings.health_file,
        settings.card_store_file,
        settings.forwarded_file,
        settings.legacy_extend_state_file,
    ):
        path = Path(raw_path)
        if path.exists():
            load_json_object(path)
        parent = path.parent
        if not parent.exists() or not os.access(parent, os.W_OK):
            raise RuntimeError(f"state directory is not writable: {parent}")

    sensitive_paths: list[str | Path] = [settings.weibo_cookie_file]
    if config_path is not None:
        sensitive_paths.append(config_path)
    for raw_path in sensitive_paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        if path.stat().st_mode & 0o077:
            raise RuntimeError(f"sensitive file permissions must be 0600: {path}")

    if settings.weibo_source in ("official_cli", "hybrid"):
        check_cli_install(settings)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        executable = Path(playwright.chromium.executable_path)
        if not executable.exists():
            raise RuntimeError(f"Playwright Chromium is not installed: {executable}")
    print(f"self-check ok: accounts={len(accounts)}")


async def _probe(settings: Settings, uid: str | None) -> int:
    accounts = load_accounts(settings.accounts_file)
    account = accounts[0]
    if uid:
        account = next((item for item in accounts if item.uid == uid), None)
        if account is None:
            logging.getLogger(__name__).error("probe uid is not configured: %s", uid)
            return 1

    try:
        async with httpx.AsyncClient() as http_client:
            if settings.weibo_source == "official_cli":
                client = OfficialCliClient(settings, read_only=True)
                data = await client.probe(account.uid)
            else:
                client = WeiboClient(settings, http_client)
                await client.ensure_cookie()
                data = await client.timeline_page(account.uid, 1)
            count = len(extract_mblogs(data))
    except RateLimitedError as exc:
        logging.getLogger(__name__).warning("probe rate limited: %s", exc)
        return 2
    except Exception as exc:
        logging.getLogger(__name__).error("probe failed: %s", exc)
        return 1
    print(f"probe ok: uid={account.uid} posts={count}")
    return 0


async def _source_check(settings: Settings) -> int:
    # hybrid 的兜底源必须随时可用，与 official_cli 同样检查 CLI 就绪
    if settings.weibo_source not in ("official_cli", "hybrid"):
        print("source-check ok: mobile source configured")
        return 0
    try:
        client = OfficialCliClient(settings, read_only=True)
        await client.source_check()
    except RateLimitedError as exc:
        logging.getLogger(__name__).warning("source-check rate limited: %s", exc)
        return 2
    except Exception as exc:
        logging.getLogger(__name__).error("source-check failed: %s", exc)
        return 1
    print("source-check ok: official CLI ready and batch timeline allowed")
    return 0


async def _source_check_and_probe(settings: Settings, uid: str | None) -> int:
    result = await _source_check(settings)
    if result != 0:
        return result
    return await _probe(settings, uid)


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
    parser.add_argument(
        "--self-check", action="store_true", help="validate local runtime without network access"
    )
    parser.add_argument(
        "--source-check",
        action="store_true",
        help="validate official CLI authentication, subscription and command access",
    )
    parser.add_argument(
        "--probe",
        nargs="?",
        const="",
        metavar="UID",
        help="fetch one configured account without sending or writing state",
    )
    args = parser.parse_args()

    config_path = args.config or ("config.yaml" if Path("config.yaml").exists() else None)
    settings = Settings.from_yaml(config_path) if config_path else Settings()

    log.setup(
        level=settings.log_level,
        log_dir=settings.log_dir,
        console_log=settings.console_log,
    )

    if args.self_check:
        try:
            _self_check(settings, config_path)
        except Exception as exc:
            logging.getLogger(__name__).critical("self-check failed: %s", exc)
            sys.exit(1)
        return

    if args.source_check:
        if args.probe is not None:
            sys.exit(
                asyncio.run(_source_check_and_probe(settings, args.probe or None))
            )
        sys.exit(asyncio.run(_source_check(settings)))

    if args.probe is not None:
        sys.exit(asyncio.run(_probe(settings, args.probe or None)))

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
