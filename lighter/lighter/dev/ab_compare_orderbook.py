from __future__ import annotations

"""
Developer tool: compare 'direct return' vs 'wrapped' OrderBookDeltas for sample frames.

Usage:
    python -m nautilus_trader.adapters.lighter.dev.ab_compare_orderbook sample.json

The sample JSON should be a list of WS messages for order_book channels.
This tool reconstructs the former wrapper behavior and compares with current.
"""

import json
import sys
from typing import Any

from nautilus_trader.adapters.lighter.schemas.ws import (
    LighterWsOrderBookMsg,
    LighterWsOrderBookSubscribedMsg,
    LighterOrderBookPayload,
    LighterOrderBookLevel,
)
from nautilus_trader.model.data import OrderBookDeltas, OrderBookDelta
from nautilus_trader.model.identifiers import InstrumentId


def _legacy_wrap(instrument_id: InstrumentId, deltas: list[OrderBookDelta]) -> OrderBookDeltas:
    # Equivalent to old _combine wrapper
    return OrderBookDeltas(instrument_id, deltas)


def _parse_one(obj: dict[str, Any], instrument_id: InstrumentId, ts_ns: int) -> tuple[OrderBookDeltas, OrderBookDeltas]:
    # Build typed struct then parse via current impl
    if obj.get("type") == "subscribed/order_book":
        payload = obj.get("order_book") or {}
        typed = LighterWsOrderBookSubscribedMsg(
            channel=obj["channel"],
            order_book=LighterOrderBookPayload(
                asks=[LighterOrderBookLevel(**x) for x in (payload.get("asks") or [])],
                bids=[LighterOrderBookLevel(**x) for x in (payload.get("bids") or [])],
            ),
            type=obj["type"],
        )
        direct = typed.parse_to_deltas(instrument_id, ts_ns)
        # legacy
        legacy = _legacy_wrap(instrument_id, list(direct.deltas))
        return direct, legacy
    else:
        payload = obj.get("order_book") or {}
        typed = LighterWsOrderBookMsg(
            channel=obj["channel"],
            offset=int(obj.get("offset") or obj.get("order_book", {}).get("offset") or 0),
            order_book=LighterOrderBookPayload(
                asks=[LighterOrderBookLevel(**x) for x in (payload.get("asks") or [])],
                bids=[LighterOrderBookLevel(**x) for x in (payload.get("bids") or [])],
            ),
            type=obj["type"],
        )
        direct = typed.parse_to_deltas(instrument_id, ts_ns)
        legacy = _legacy_wrap(instrument_id, list(direct.deltas))
        return direct, legacy


def _equal(a: OrderBookDeltas, b: OrderBookDeltas) -> bool:
    if a.instrument_id != b.instrument_id:
        return False
    if len(a.deltas) != len(b.deltas):
        return False
    for x, y in zip(a.deltas, b.deltas):
        if (
            x.action != y.action
            or x.sequence != y.sequence
            or x.flags != y.flags
            or bool(x.is_clear) != bool(y.is_clear)
        ):
            return False
        # Compare order fields if present
        if x.order is None and y.order is None:
            continue
        if (x.order is None) != (y.order is None):
            return False
        assert x.order and y.order
        if (x.order.is_ask != y.order.is_ask) or (str(x.order.price) != str(y.order.price)) or (str(x.order.size) != str(y.order.size)):
            return False
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: ab_compare_orderbook.py sample.json")
        return 2
    path = sys.argv[1]
    msgs = json.loads(open(path, "r", encoding="utf-8").read())
    iid = InstrumentId.from_str("BTC-USD-PERP.LIGHTER")
    mismatches = 0
    for i, obj in enumerate(msgs):
        d, l = _parse_one(obj, iid, ts_ns=0)
        if not _equal(d, l):
            print(f"Mismatch at index {i}")
            mismatches += 1
    if mismatches:
        print(f"Found {mismatches} mismatches")
        return 1
    print("All messages equivalent (direct vs legacy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

