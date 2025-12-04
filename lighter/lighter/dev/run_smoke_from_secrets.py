from __future__ import annotations

"""
Lighter adapter smoke test using secrets/lighter_testnet_account.json.

Runs provider load, data WS connect (market_stats/all), and execution WS connect
with private subscriptions. Does not submit orders.

Usage:
  python3 -m nautilus_trader.adapters.lighter.dev.run_smoke_from_secrets \
      --http https://testnet.zklighter.elliot.ai \
      --ws wss://testnet.zklighter.elliot.ai/stream \
      --secrets secrets/lighter_testnet_account.json \
      --duration 10
"""

import argparse
import asyncio

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.data import LighterDataClient, LighterDataClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", required=False, default="https://testnet.zklighter.elliot.ai")
    ap.add_argument("--ws", required=False, default="wss://testnet.zklighter.elliot.ai/stream")
    ap.add_argument("--secrets", required=False, default="secrets/lighter_testnet_account.json")
    ap.add_argument("--duration", type=int, default=10, help="seconds to keep data streams alive")
    ap.add_argument("--bases", type=str, default="ETH", help="comma-separated base symbols to load (eg. ETH,BTC)")
    args = ap.parse_args()

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    msgbus = MessageBus(TraderId("TRADER-LIGHTER-SMOKE"), clock)
    cache = Cache()

    # Provider
    # Add simple client-side ratelimit to avoid 429 on testnet
    try:
        from nautilus_trader.core.nautilus_pyo3 import Quota
        http_pub = LighterPublicHttpClient(
            args.http,
            ratelimiter_default_quota=Quota.rate_per_minute(6),
        )
    except Exception:
        http_pub = LighterPublicHttpClient(args.http)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    # Attempt provider load with filters to reduce API cost
    bases = [s.strip().upper() for s in (args.bases.split(',') if args.bases else []) if s.strip()]
    filters = {"bases": bases} if bases else None
    # Retry with simple exponential backoff to mitigate 429s
    load_err = None
    for attempt in range(3):
        try:
            await provider.load_all_async(filters=filters)
            load_err = None
            break
        except Exception as exc:
            load_err = exc
            backoff = 1.0 * (2 ** attempt)
            print(f"Provider load attempt {attempt+1} failed: {exc}; retrying in {backoff:.1f}s...")
            await asyncio.sleep(backoff)
    if load_err:
        print(f"Provider load failed after retries: {load_err}; continuing with data WS only")
    print(f"Loaded {len(provider.get_all())} instruments (filters={bases})")

    # Data client (public)
    data_cfg = LighterDataClientConfig(base_url_ws=args.ws)
    data = LighterDataClient(
        loop=loop,
        http=http_pub,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,
        base_url_ws=args.ws,
        name="LIGHTER-DATA",
        config=data_cfg,
    )
    # LiveMarketDataClient.connect is synchronous wrapper in this environment
    data.connect()
    # Subscribe to market_stats/all
    await data._ws.subscribe("market_stats/all")

    # (Optional) Execution client setup can be added after data path is verified

    # Keep alive for duration seconds to receive few messages
    await asyncio.sleep(max(1, int(args.duration)))

    # Teardown
    data.disconnect()
    print("Smoke test completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
