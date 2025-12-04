from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from scripts.lighter_tests.context import LighterTestContext, configure_logging


logger = logging.getLogger("lighter_test.monitor")


async def poll_account(context: LighterTestContext, interval: int) -> None:
    while True:
        try:
            token = context.auth_token()
            overview = await context.http_account.get_account_overview(account_index=context.credentials.account_index, auth=token)
            logger.info("overview balances=%s", overview.get("available_balance"))
            positions = await context.http_account.get_account_positions(context.credentials.account_index, auth=token)
            logger.info("positions count=%s", len(positions))
            orders = await context.http_account.get_account_orders(context.credentials.account_index, auth=token)
            logger.info("orders count=%s", len(orders))
        except Exception as exc:
            logger.error("poll_account error: %s", exc)
        await asyncio.sleep(interval)


async def stream_ws(context: LighterTestContext, run_secs: int) -> None:
    async def handler(msg: dict) -> None:
        t = msg.get("type")
        if t == "connected":
            logger.info("WS connected %s", msg.get("session_id"))
            return
        if t == "ping":
            logger.debug("WS ping")
            return
        ch = msg.get("channel")
        if ch:
            logger.info("WS %s update keys=%s", ch, list((msg.get("data") or {}).keys()))
        else:
            logger.info("WS msg=%s", msg)

    token = context.auth_token()
    subs = [
        {"type": "subscribe", "channel": "market_stats/all"},
        {"type": "subscribe", "channel": f"account_all/{context.credentials.account_index}", "auth": token},
        {"type": "subscribe", "channel": f"account_all_orders/{context.credentials.account_index}", "auth": token},
        {"type": "subscribe", "channel": f"account_all_positions/{context.credentials.account_index}", "auth": token},
    ]
    await context.run_ws_session(handler=handler, subscriptions=subs, run_secs=run_secs)


async def main_async(context: LighterTestContext, interval: int, ws_secs: int) -> None:
    await asyncio.gather(
        poll_account(context, interval),
        stream_ws(context, ws_secs),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Lighter monitor utility")
    parser.add_argument("--credentials", default=os.getenv("LIGHTER_CREDENTIALS_JSON", "secrets/lighter_testnet_account.json"))
    parser.add_argument("--http", default=os.getenv("LIGHTER_HTTP", "https://testnet.zklighter.elliot.ai"))
    parser.add_argument("--ws", default=os.getenv("LIGHTER_WS"))
    parser.add_argument("--interval", type=int, default=int(os.getenv("LIGHTER_MONITOR_INTERVAL", "30")))
    parser.add_argument("--ws-runtime", type=int, default=int(os.getenv("LIGHTER_MONITOR_WS_SECS", "300")))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=not args.quiet)
    context = LighterTestContext(
        credentials_path=Path(args.credentials),
        base_http=args.http,
        base_ws=args.ws,
    )

    try:
        asyncio.run(main_async(context, args.interval, args.ws_runtime))
    except KeyboardInterrupt:
        logger.info("monitor stopped by user")


if __name__ == "__main__":
    main()
