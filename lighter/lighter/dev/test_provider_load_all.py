from __future__ import annotations

"""
Offline provider test using a mocked HTTP client to validate load_all_async.

Run:
  python -m nautilus_trader.adapters.lighter.dev.test_provider_load_all
"""

import asyncio
from typing import Any

from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig


class _MockPublicHttp(LighterPublicHttpClient):
    def __init__(self) -> None:  # type: ignore[override]
        # Bypass real parent init
        self._base_url = "mock://lighter"
        self._headers = {}
        self._decoder = None
        self._timeout_secs = 0
        self._retry_initial_ms = 0
        self._retry_max_ms = 0
        self._retry_backoff_factor = 0.0
        self._max_retries = 0

    async def get_order_books(self):  # type: ignore[override]
        return [
            {
                "symbol": "ETH",
                "market_id": 1,
                "status": "active",
                "maker_fee": "0.0002",
                "taker_fee": "0.0005",
                "min_base_amount": "0.001",
                "min_quote_amount": "1",
                "supported_size_decimals": 3,
                "supported_price_decimals": 2,
                "supported_quote_decimals": 6,
            },
            {
                "symbol": "BTC",
                "market_id": 2,
                "status": "active",
                "maker_fee": "0.0002",
                "taker_fee": "0.0005",
                "min_base_amount": "0.0001",
                "min_quote_amount": "1",
                "supported_size_decimals": 4,
                "supported_price_decimals": 1,
                "supported_quote_decimals": 6,
            },
        ]

    async def get_order_book_details(self, market_id: int):  # type: ignore[override]
        details: dict[int, dict[str, Any]] = {
            1: {
                "market_id": 1,
                "price_decimals": 2,
                "size_decimals": 3,
                "quote_multiplier": 1,
                "default_initial_margin_fraction": "0.1",
                "maintenance_margin_fraction": "0.05",
            },
            2: {
                "market_id": 2,
                "price_decimals": 1,
                "size_decimals": 4,
                "quote_multiplier": 1,
                "default_initial_margin_fraction": "1000",  # bps form
                "maintenance_margin_fraction": "500",    # bps form
            },
        }
        return details.get(market_id)


async def _run() -> int:
    http = _MockPublicHttp()
    provider = LighterInstrumentProvider(client=http, config=InstrumentProviderConfig(), concurrency=1)
    await provider.load_all_async()
    instruments = provider.get_all()
    if not instruments:
        print("FAIL: provider returned no instruments")
        return 2
    print(f"OK: loaded {len(instruments)} instruments")
    # Print a brief summary
    for iid, inst in instruments.items():
        info = inst.info
        print(" -", iid, "mid=", info.get("market_id"), "px_dec=", inst.price_precision, "sz_dec=", inst.size_precision)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))

