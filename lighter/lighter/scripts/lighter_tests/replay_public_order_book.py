#!/usr/bin/env python3
from __future__ import annotations

"""
Offline replay of recorded public Lighter order book messages into a Nautilus
`OrderBook`, to verify that:

- `LighterWsOrderBookSubscribedMsg.parse_to_deltas` and
  `LighterWsOrderBookMsg.parse_to_deltas` produce sensible `OrderBookDeltas`.
- The core `OrderBook` builder (apply_deltas) reconstructs a usable book
  (best bid/ask, depth) from the same deltas that `LighterDataClient` would
  emit in live mode.

Usage
-----

  PYTHONPATH=/root/myenv/lib/python3.12/site-packages \\
    python adapters/lighter/scripts/lighter_tests/replay_public_order_book.py \\
      --file /tmp/lighter_public_bnb.jsonl \\
      --market-id 25 \\
      --instrument BNBUSDC-PERP.LIGHTER

The script prints periodic snapshots of top-of-book and depth as deltas are
applied, so we can compare with live strategy behaviour.
"""

import argparse
import json
from pathlib import Path

import msgspec

from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.identifiers import InstrumentId

from nautilus_trader.adapters.lighter.schemas.ws import (
    LighterWsOrderBookSubscribedMsg,
    LighterWsOrderBookMsg,
)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Replay recorded Lighter public order book into OrderBook")
    ap.add_argument(
        "--file",
        type=Path,
        default=Path("/tmp/lighter_public_bnb.jsonl"),
        help="Path to JSONL file recorded by record_public_ws.py",
    )
    ap.add_argument(
        "--market-id",
        type=int,
        default=25,
        help="Lighter market_id to filter (e.g. 25 for BNBUSDC-PERP)",
    )
    ap.add_argument(
        "--instrument",
        type=str,
        default="BNBUSDC-PERP.LIGHTER",
        help="InstrumentId string corresponding to the market_id",
    )
    ap.add_argument(
        "--max-lines",
        type=int,
        default=2000,
        help="Maximum number of JSONL lines to replay (0 = no limit)",
    )
    ap.add_argument(
        "--print-every",
        type=int,
        default=20,
        help="Print book snapshot every N applied messages",
    )
    return ap.parse_args()


def main() -> int:
    args = _parse_args()
    path: Path = args.file
    if not path.exists():
        print(f"ERROR: file not found: {path}")
        return 1

    inst_id = InstrumentId.from_str(args.instrument)
    # Depth 0 = "full book" semantics (matches typical DataEngine behaviour).
    book = OrderBook(inst_id, 0)

    dec_sub = msgspec.json.Decoder(LighterWsOrderBookSubscribedMsg)
    dec_upd = msgspec.json.Decoder(LighterWsOrderBookMsg)

    applied = 0
    snapshots = 0
    updates = 0

    print(f"Replaying file={path} market_id={args.market_id} instrument={inst_id} ...")

    with path.open("r", encoding="utf-8") as fh:
        for idx, line in enumerate(fh, start=1):
            if args.max_lines and idx > args.max_lines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                outer = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = outer.get("msg") or outer
            if not isinstance(msg, dict):
                continue

            channel = msg.get("channel") or ""
            if channel != f"order_book:{args.market_id}":
                continue

            msg_type = msg.get("type") or ""
            raw = json.dumps(msg).encode("utf-8")

            if msg_type == "subscribed/order_book":
                try:
                    m = dec_sub.decode(raw)
                except Exception as exc:
                    print(f"[{idx}] decode snapshot error: {exc}")
                    continue
                deltas = m.parse_to_deltas(inst_id, outer.get("ts", 0))
                book.apply(deltas)  # type: ignore[arg-type]
                snapshots += 1
            elif msg_type.endswith("order_book"):
                try:
                    m = dec_upd.decode(raw)
                except Exception as exc:
                    print(f"[{idx}] decode update error: {exc}")
                    continue
                deltas = m.parse_to_deltas(inst_id, outer.get("ts", 0))
                book.apply(deltas)  # type: ignore[arg-type]
                updates += 1
            else:
                continue

            applied += 1

            if applied % args.print_every == 0:
                try:
                    has_spread = book.spread()
                except Exception:
                    has_spread = False
                try:
                    best_bid = book.best_bid_price() if has_spread else None
                    best_ask = book.best_ask_price() if has_spread else None
                except Exception:
                    best_bid = None
                    best_ask = None
                # Depth counts (defensive)
                try:
                    nbids = len(book.bids())
                    nasks = len(book.asks())
                except Exception:
                    nbids = -1
                    nasks = -1

                print(
                    f"[{idx}] applied={applied} snap={snapshots} upd={updates} "
                    f"spread={has_spread} best_bid={best_bid} best_ask={best_ask} "
                    f"nbids={nbids} nasks={nasks}"
                )

    # final snapshot
    try:
        has_spread = book.spread()
    except Exception:
        has_spread = False
    try:
        best_bid = book.best_bid_price() if has_spread else None
        best_ask = book.best_ask_price() if has_spread else None
    except Exception:
        best_bid = None
        best_ask = None
    try:
        nbids = len(book.bids())
        nasks = len(book.asks())
    except Exception:
        nbids = -1
        nasks = -1

    print(
        f"Done. applied={applied} snap={snapshots} upd={updates} "
        f"spread={has_spread} best_bid={best_bid} best_ask={best_ask} "
        f"nbids={nbids} nasks={nasks}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
