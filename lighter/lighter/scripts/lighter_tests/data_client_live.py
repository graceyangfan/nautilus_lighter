#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import msgspec

from nautilus_trader.adapters.lighter.common.types import LighterMarketStats
from nautilus_trader.adapters.lighter.data import LighterDataClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.data import CustomData, OrderBookDeltas, TradeTick
from nautilus_trader.model.identifiers import InstrumentId, TraderId


async def load_single_instrument(http: LighterPublicHttpClient, symbol_prefix: str | None) -> tuple[LighterInstrumentProvider, InstrumentId]:
    provider = LighterInstrumentProvider(client=http, config=InstrumentProviderConfig(load_all=False), concurrency=2)
    filters = None
    if symbol_prefix:
        base = symbol_prefix.upper()
        filters = {"symbols": [f"{base}USDC-PERP"]}
    await provider.load_all_async(filters)
    instruments = list(provider.get_all().values())
    if not instruments:
        raise RuntimeError("No instruments loaded using provider filters")
    return provider, instruments[0].id


async def run_live(http_url: str, ws_url: str, secrets: Path | None, runtime: int, symbol: str | None) -> None:
    loop = asyncio.get_event_loop()
    clock = LiveClock()
    msgbus = MessageBus(TraderId("TRADER-LIGHTER"), clock)
    cache = Cache()

    http = LighterPublicHttpClient(http_url)
    # Load a single instrument via Provider (filters to minimize HTTP)
    provider, inst_id = await load_single_instrument(http, symbol)

    client = LighterDataClient(
        loop=loop,
        http=http,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,  # type: ignore[arg-type]
        base_url_ws=ws_url,
        name="LIGHTER",
    )

    # Collect and pretty print a sample of events
    stats_count = 0
    book_count = 0
    trade_count = 0
    max_each = 5

    orig_handle = client._handle_data  # type: ignore[attr-defined]

    def handle_sampled(data: Any) -> None:
        nonlocal stats_count, book_count, trade_count
        if isinstance(data, CustomData) and isinstance(data.data, LighterMarketStats):
            if stats_count < max_each:
                print("[stats]", data.data.instrument_id, {
                    k: getattr(data.data, k) for k in (
                        "index_price", "mark_price", "open_interest", "last_trade_price",
                    )
                })
            stats_count += 1
        elif isinstance(data, OrderBookDeltas):
            if book_count < max_each:
                print("[book]", data.instrument_id, f"deltas={len(data.deltas)}")
            book_count += 1
        elif isinstance(data, TradeTick):
            if trade_count < max_each:
                print("[trade]", data.instrument_id, data.price.as_double(), data.size.as_double(), data.aggressor_side)
            trade_count += 1
        orig_handle(data)

    client._handle_data = handle_sampled  # type: ignore[method-assign]

    # Connect and subscribe
    await client._connect()  # direct await for script simplicity
    # Patch ws reconnect handler to async no-op to avoid "await None" in wrapper
    async def _noop():
        return None
    try:
        client._ws._handler_reconnect = _noop  # type: ignore[attr-defined]
    except Exception:
        pass

    # Use WS client directly to avoid constructing Cython Subscribe* messages here
    await client._ws.wait_until_ready(10)
    await client._ws.subscribe("market_stats/all")
    # Order book + trades for chosen instrument
    market_id = int(client._instrument_provider.get_all()[inst_id].info["market_id"])  # type: ignore[index]
    await client._ws.subscribe(f"order_book/{market_id}")
    await client._ws.subscribe(f"trade/{market_id}")

    print(f"Subscribed. Running for {runtime}s on {inst_id} ...")
    await asyncio.sleep(runtime)

    client.disconnect()
    print("Done. Sample counts:", {
        "stats": stats_count,
        "book": book_count,
        "trade": trade_count,
    })


def main() -> None:
    ap = argparse.ArgumentParser(description="LighterDataClient live subscription test")
    ap.add_argument("--http", type=str, default="https://testnet.zklighter.elliot.ai")
    ap.add_argument("--ws", type=str, default="wss://testnet.zklighter.elliot.ai/stream")
    ap.add_argument("--secrets", type=Path, default=Path("secrets/lighter_testnet_account.json"))
    ap.add_argument("--runtime", type=int, default=20)
    ap.add_argument("--symbol", type=str, default=None, help="optional symbol prefix, e.g. BTC or ETH")
    args = ap.parse_args()

    # Optional: show that secrets is present (not required for public data)
    if args.secrets.exists():
        try:
            data = json.loads(args.secrets.read_text(encoding="utf-8"))
            print("Loaded secrets pubkey prefix:", str(data.get("pubkey", ""))[:10])
        except Exception:
            pass

    asyncio.run(run_live(args.http, args.ws, args.secrets if args.secrets.exists() else None, args.runtime, args.symbol))


if __name__ == "__main__":
    main()
