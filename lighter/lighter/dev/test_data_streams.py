from __future__ import annotations

"""
Live test for LighterDataClient parsing of market_stats, order_book, and trades.

Usage:
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python -m nautilus_trader.adapters.lighter.dev.test_data_streams \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --bases ETH,BTC \
    --runtime 300 \
    --print-raw
"""

import argparse
import asyncio
from typing import Any

from nautilus_trader.adapters.lighter.config import LighterDataClientConfig, LighterRateLimitConfig
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.data import LighterDataClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", default="https://testnet.zklighter.elliot.ai")
    ap.add_argument("--ws", default="wss://testnet.zklighter.elliot.ai/stream")
    ap.add_argument("--bases", default="ETH,BTC", help="comma-separated base symbols to load (eg. ETH,BTC)")
    ap.add_argument("--runtime", type=int, default=300)
    ap.add_argument("--print-raw", action="store_true", help="print raw WS frames (truncated)")
    args = ap.parse_args()

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    msgbus = MessageBus(TraderId("TRADER-DATA-TEST"), clock)
    cache = Cache()

    http = LighterPublicHttpClient(args.http)
    provider = LighterInstrumentProvider(client=http, concurrency=1)
    bases = [s.strip().upper() for s in args.bases.split(",") if s.strip()]
    filters = {"bases": bases} if bases else None
    await provider.load_all_async(filters=filters)
    inst_map = provider.get_all()
    if not inst_map:
        print("No instruments loaded (check --bases or network)")
        return 1
    # Build market list (limit to first 5 to reduce noise)
    selected = list(inst_map.items())[:5]
    mids = []
    for inst_id, inst in selected:
        mid = int(inst.info["market_id"])  # fail-fast
        mids.append((inst_id, mid))
    print("Using instruments:")
    for iid, mid in mids:
        print(" -", iid, "market_id=", mid)

    cfg = LighterDataClientConfig(base_url_ws=args.ws, ratelimit=LighterRateLimitConfig(ws_send_per_second=3))
    client = LighterDataClient(
        loop=loop,
        http=http,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,
        base_url_ws=args.ws,
        name="LIGHTER-DATA-TEST",
        config=cfg,
    )

    # Patch handler to print simple summaries
    counts = {"stats": 0, "book": 0, "trade": 0}

    def _printer(data: Any) -> None:
        # Import locally to avoid heavy imports at module import time
        from nautilus_trader.model.data import CustomData, OrderBookDeltas, TradeTick
        tname = type(data).__name__
        # Stats wrapped as CustomData(LighterMarketStats)
        if isinstance(data, CustomData):
            payload = data.data
            if payload is not None and type(payload).__name__.endswith("LighterMarketStats"):
                counts["stats"] += 1
                print("[stats]", payload.index_price, payload.mark_price)
                return
        if tname.endswith("LighterMarketStats"):
            counts["stats"] += 1
            print("[stats]", data.index_price, data.mark_price)
            return
        if isinstance(data, OrderBookDeltas):
            counts["book"] += 1
            ds = data.deltas
            print("[book]", len(ds), "deltas", "clear" if (ds and ds[0].is_clear) else "update")
            return
        if isinstance(data, TradeTick):
            counts["trade"] += 1
            print("[trade]", data.price, data.size)
            return
        # Fallback: print type only for debugging
        print("[other]", tname)

    client._handle_data = _printer  # type: ignore[method-assign]

    # Optionally print raw frames by wrapping WS handler
    if args.print_raw:
        old = client._ws._handler
        def _raw_wrapper(raw: bytes) -> None:
            print("[raw]", raw[:200])
            old(raw)
        client._ws._handler = _raw_wrapper

    # Connect and subscribe raw channels to exercise data.py parsing
    client.connect()
    # Ensure WS handshake completes and mapping is present
    await client._ws.wait_until_ready(10)
    client._build_market_mapping()
    # Subscribe all stats + per-market channels (use slash format for server)
    await client._ws.subscribe("market_stats/all")
    for _, mid in mids:
        await client._ws.subscribe(f"market_stats/{mid}")
        await client._ws.subscribe(f"order_book/{mid}")
        await client._ws.subscribe(f"trade/{mid}")
    print("Subscribed market_stats/all and per-market channels for", len(mids), "markets")

    await asyncio.sleep(max(1, int(args.runtime)))
    client.disconnect()
    print("Received counts:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
