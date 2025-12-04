from __future__ import annotations

"""
Minimal auth check for LighterExecutionClient using provided secrets.

Prints signer.available, auth token length, and attempts to connect + subscribe
private channels for a short window, writing diagnostics if LIGHTER_EXEC_LOG is set.

Usage:
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  LIGHTER_EXEC_LOG=/tmp/lighter_exec_auth_diag.log \
  python -m nautilus_trader.adapters.lighter.dev.exec_auth_check \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --runtime 20
"""

import argparse
import asyncio

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secrets", required=True)
    ap.add_argument("--http", required=True)
    ap.add_argument("--ws", required=True)
    ap.add_argument("--runtime", type=int, default=20)
    args = ap.parse_args()

    clock = LiveClock()
    msgbus = MessageBus(TraderId("TR-EXEC-AUTH-TEST"), clock)
    cache = Cache()

    creds = LighterCredentials.from_json_file(args.secrets)
    print("secrets loaded: account_index=", creds.account_index, "api_key_index=", creds.api_key_index)

    # signer availability + token length (via execution client after init)
    provider = LighterInstrumentProvider(LighterPublicHttpClient(args.http), concurrency=1)
    await provider.load_all_async()
    cfg = LighterExecClientConfig(base_url_http=args.http, base_url_ws=args.ws, credentials=creds, chain_id=300, subscribe_account_stats=True, subscribe_split_account_channels=True)
    exec_client = LighterExecutionClient(
        loop=asyncio.get_event_loop(),
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,
        base_url_http=args.http,
        base_url_ws=args.ws,
        credentials=creds,
        config=cfg,
    )
    # Access signer
    signer = exec_client._signer
    print("signer.available=", signer.available())
    tok = signer.create_auth_token()
    print("auth_token_length=", len(tok) if tok else 0)

    # Connect and wait for a short window
    await exec_client._connect()
    print("connected; waiting", args.runtime, "seconds")
    await asyncio.sleep(max(1, int(args.runtime)))
    await exec_client._disconnect()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
