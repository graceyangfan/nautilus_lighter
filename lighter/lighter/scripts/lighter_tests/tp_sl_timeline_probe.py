#!/usr/bin/env python3
from __future__ import annotations

"""
Analyze a JSONL private WS recording and print TP/SL order lifecycles:
- Order states (status, trigger_status),
- Linked trades (by order_index / ask_id / bid_id),
- Position snapshots before/after.

Usage:
  python scripts/lighter_tests/tp_sl_timeline_probe.py /tmp/lighter_tp_sl.jsonl --market 1
"""

import argparse
import json
from collections import defaultdict
from typing import Any


def to_list(x: Any) -> list:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        # values may be lists or objects
        vals = []
        for v in x.values():
            if isinstance(v, list):
                vals.extend(v)
            else:
                vals.append(v)
        return vals
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", help="JSONL recording from record_private_ws or tp_sl_roundtrip")
    ap.add_argument("--market", type=int, default=None, help="filter by market_id/index")
    args = ap.parse_args()

    order_events: dict[int, list[tuple[int, dict]]] = defaultdict(list)
    trades_by_order: dict[int, list[tuple[int, dict]]] = defaultdict(list)
    positions: list[tuple[int, dict]] = []

    with open(args.jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            ts = int(obj.get("ts") or 0)
            msg = obj.get("msg") if isinstance(obj, dict) else None
            if msg is None and isinstance(obj, dict):
                msg = obj
            if not isinstance(msg, dict):
                continue
            ch = str(msg.get("channel") or "")
            t = str(msg.get("type") or "")
            data = msg.get("data") if isinstance(msg.get("data"), dict) else msg
            # Orders
            if ch.startswith("account_all_orders:") or t.endswith("account_all_orders") or (isinstance(data, dict) and data.get("orders") is not None):
                orders = to_list(data.get("orders"))
                for od in orders:
                    try:
                        mid = int(od.get("market_index") or od.get("market_id") or -1)
                        if args.market is not None and mid != int(args.market):
                            continue
                        oi = int(od.get("order_index"))
                    except Exception:
                        continue
                    order_events[oi].append((ts, od))
            # Trades
            if ch.startswith("account_all_trades:") or (isinstance(data, dict) and data.get("trades") is not None):
                lst = to_list(data.get("trades"))
                for tr in lst:
                    # try link by order_index or ask_id/bid_id
                    linked = None
                    for key in ("order_index", "ask_id", "bid_id"):
                        val = tr.get(key)
                        try:
                            if val is not None:
                                oi = int(val)
                                linked = oi
                                break
                        except Exception:
                            continue
                    if linked is not None:
                        trades_by_order[linked].append((ts, tr))
            # Positions
            if ch.startswith("account_all_positions:") or (isinstance(data, dict) and data.get("positions") is not None):
                pos_map = data.get("positions")
                if isinstance(pos_map, dict):
                    for v in pos_map.values():
                        try:
                            mid = int(v.get("market_index") or v.get("market_id") or -1)
                            if args.market is not None and mid != int(args.market):
                                continue
                            positions.append((ts, v))
                        except Exception:
                            continue

    # Print summary
    print("Orders tracked:", len(order_events))
    for oi in sorted(order_events.keys()):
        print("\nOrder", oi)
        events = sorted(order_events[oi], key=lambda x: x[0])
        for ts, od in events:
            status = od.get("status")
            trig = od.get("trigger_status")
            tif = od.get("time_in_force")
            otype = od.get("type")
            ro = od.get("reduce_only")
            is_ask = od.get("is_ask")
            print(f"  [{ts}] status={status} trigger={trig} tif={tif} type={otype} ro={ro} is_ask={is_ask}")
        if oi in trades_by_order:
            tlist = sorted(trades_by_order[oi], key=lambda x: x[0])
            for ts, tr in tlist:
                tid = tr.get("trade_id")
                px = tr.get("price")
                sz = tr.get("size")
                print(f"    trade ts={ts} id={tid} px={px} sz={sz}")
    # Position snapshots (filtered)
    if positions:
        print("\nPositions (filtered by --market if provided):", len(positions))
        for ts, pv in sorted(positions, key=lambda x: x[0]):
            size = pv.get("position")
            aep = pv.get("avg_entry_price")
            mid = pv.get("market_index") or pv.get("market_id")
            print(f"  [{ts}] market={mid} size={size} avg_entry={aep}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

