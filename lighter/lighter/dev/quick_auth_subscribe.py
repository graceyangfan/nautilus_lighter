from __future__ import annotations

"""
Minimal signer + subscription check for Lighter testnet.

This script avoids any REST catalog calls (no provider), and only:
  1) Loads credentials JSON
  2) Creates a signer and fetches an auth token
  3) Connects WS and subscribes to private channels with the token

Run (inside your venv):
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python -m nautilus_trader.adapters.lighter.dev.quick_auth_subscribe \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --http https://testnet.zklighter.elliot.ai \
    --runtime 30
"""

import argparse
import asyncio
import logging

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner
from nautilus_trader.adapters.lighter.websocket.client import LighterWebSocketClient
from nautilus_trader.common.component import LiveClock, Logger
from nautilus_trader.common.enums import LogColor


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secrets", required=True)
    parser.add_argument("--ws", required=True)
    parser.add_argument("--http", required=True)
    parser.add_argument("--runtime", type=int, default=20)
    args = parser.parse_args()

    creds = LighterCredentials.from_json_file(args.secrets)
    signer = LighterSigner(creds, base_url=args.http, chain_id=300)
    print("Signer available:", signer.available())
    tok = signer.create_auth_token(deadline_seconds=600)
    print("Auth token length:", len(tok))
    if not tok:
        print("WARNING: empty auth token; subscription may fail")

    clock = LiveClock()
    # Basic logger setup for WS client
    logging.getLogger().setLevel(logging.INFO)
    Logger("quick_auth").info("Connecting WS...", LogColor.BLUE)

    # Create WS client and connect
    ws = LighterWebSocketClient(
        clock=clock,
        base_url=args.ws,
        handler=lambda raw: print("WS:", raw[:120], "..."),
        handler_reconnect=None,
        loop=asyncio.get_event_loop(),
    )
    await ws.connect()
    ready = await ws.wait_until_ready(10)
    print("WS ready:", ready)

    # Subscribe private channels with token
    acct = creds.account_index
    await ws.send({"type": "subscribe", "channel": f"account_all/{acct}", "auth": tok})
    await ws.send({"type": "subscribe", "channel": f"account_all_orders/{acct}", "auth": tok})
    await ws.send({"type": "subscribe", "channel": f"account_all_positions/{acct}", "auth": tok})
    await ws.send({"type": "subscribe", "channel": f"account_stats/{acct}", "auth": tok})

    await asyncio.sleep(max(1, int(args.runtime)))
    await ws.disconnect()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
