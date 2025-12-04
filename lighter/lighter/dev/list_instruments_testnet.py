from __future__ import annotations

"""
Fetch and list all supported instruments from Lighter testnet with cautious throttling.

This avoids provider concurrency by fetching summaries and details with
manual backoff to reduce 429 responses, then reuses the provider mapper
to construct Instrument objects.

Usage:
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python -m nautilus_trader.adapters.lighter.dev.list_instruments_testnet \
    --http https://testnet.zklighter.elliot.ai
"""

import argparse
import asyncio
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.http.errors import LighterClientError
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider, _OrderBookSummary
from nautilus_trader.core.nautilus_pyo3 import Quota


async def _backoff_get_summaries(http: LighterPublicHttpClient) -> list[dict]:
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            return await http.get_order_books()
        except LighterClientError as exc:
            last_exc = exc
            await asyncio.sleep(1.5 * (attempt + 1))
        except Exception as exc:
            # network/transient errors are retried; surface last exception after attempts
            last_exc = exc
            await asyncio.sleep(1.5 * (attempt + 1))
    if last_exc:
        raise last_exc
    return []


async def _backoff_get_details(http: LighterPublicHttpClient, market_id: int) -> dict | None:
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            return await http.get_order_book_details(market_id)
        except LighterClientError as exc:
            last_exc = exc
            await asyncio.sleep(1.5 * (attempt + 1))
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(1.5 * (attempt + 1))
    if last_exc:
        raise last_exc
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", default="https://testnet.zklighter.elliot.ai")
    args = ap.parse_args()

    quotas = [
        ("lighter:/api/v1/orderBooks", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookDetails", Quota.rate_per_minute(6)),
    ]
    http = LighterPublicHttpClient(
        base_url=args.http,
        ratelimiter_quotas=quotas,
        retry_initial_ms=200,
        retry_max_ms=3000,
        retry_backoff_factor=2.0,
        max_retries=2,
    )
    provider = LighterInstrumentProvider(client=http, concurrency=1)

    raw_books = await _backoff_get_summaries(http)
    active = [rb for rb in raw_books if (rb.get("status") or "active").lower() == "active"]
    instruments = []
    for item in active:
        try:
            symbol = str(item["symbol"]).upper()
            market_id = int(item["market_id"])
            status = str(item.get("status") or "active")
            maker_fee = Decimal(str(item.get("maker_fee", "0")))
            taker_fee = Decimal(str(item.get("taker_fee", "0")))
            min_base_amount = Decimal(str(item.get("min_base_amount", "0")))
            min_quote_amount = Decimal(str(item.get("min_quote_amount", "0")))
            supported_size_decimals = int(item.get("supported_size_decimals", 0))
            supported_price_decimals = int(item.get("supported_price_decimals", 0))
            supported_quote_decimals = int(item.get("supported_quote_decimals", 6))
        except (KeyError, TypeError, ValueError):
            continue

        details = await _backoff_get_details(http, market_id)
        if not details:
            continue
        summary = _OrderBookSummary(
            symbol=symbol,
            market_id=market_id,
            status=status,
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            min_base_amount=min_base_amount,
            min_quote_amount=min_quote_amount,
            supported_size_decimals=supported_size_decimals,
            supported_price_decimals=supported_price_decimals,
            supported_quote_decimals=supported_quote_decimals,
        )
        try:
            inst = provider._map_instrument(summary, details)  # reuse provider mapper
            instruments.append(inst)
        except (KeyError, TypeError, ValueError):
            continue
        # be gentle
        await asyncio.sleep(1.0)

    print("total instruments:", len(instruments))
    for inst in instruments:
        info = inst.info or {}
        print(inst.id, "| base=", inst.base_currency.code, "quote=", inst.quote_currency.code, "market_id=", info.get("market_id"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
