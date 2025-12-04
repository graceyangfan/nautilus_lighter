from __future__ import annotations

"""
Micro-benchmark for Lighter order book parsing.

Usage:
    python -m nautilus_trader.adapters.lighter.dev.perf_orderbook N
    # N = number of synthetic messages (default 10000)
"""

import sys
import time
from nautilus_trader.adapters.lighter.schemas.ws import (
    LighterWsOrderBookMsg,
    LighterOrderBookPayload,
    LighterOrderBookLevel,
)
from nautilus_trader.model.identifiers import InstrumentId


def run(n: int = 10000) -> float:
    iid = InstrumentId.from_str("BTC-USD-PERP.LIGHTER")
    now = time.time_ns()
    # Prepare a typical update frame
    msg = LighterWsOrderBookMsg(
        channel="order_book:1",
        offset=123,
        order_book=LighterOrderBookPayload(
            asks=[LighterOrderBookLevel(price="20000.5", size="1.25")],
            bids=[LighterOrderBookLevel(price="19999.5", size="2.00")],
        ),
        type="update/order_book",
    )
    t0 = time.perf_counter()
    c = 0
    for _ in range(n):
        deltas = msg.parse_to_deltas(iid, now)
        c += len(deltas.deltas)
    t1 = time.perf_counter()
    dur = t1 - t0
    print(f"Parsed {n} frames, {c} deltas in {dur:.4f}s -> {n/dur:.1f} fps")
    return dur


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    run(n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

