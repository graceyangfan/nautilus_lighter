#!/usr/bin/env python3
from __future__ import annotations

"""
Check BNB (market_id=25) private WS state in recorded JSONL.

This script inspects a JSONL file produced via LIGHTER_EXEC_RECORD or
record_private_ws.py, and verifies for a given market_id (default 25):

  - Positions (account_all_positions):
      * At least one position snapshot exists for that market_id.
      * The last observed position size matches `--expected-pos` (default 0).
  - Orders (account_all_orders):
      * For each client_order_index on that market_id:
          - At least one 'open' status is seen.
          - If 'canceled' status is seen, it appears exactly once and only
            after the last 'open' status for that order.

Usage examples:

  # Check BNB (market_id=25) in modify_cases recording
  PYTHONPATH=/root/myenv/lib/python3.12/site-packages \\
  python adapters/lighter/scripts/lighter_tests/check_bnb_ws_state.py \\
    --jsonl /tmp/nautilus_exec_ws_modify_cases_latest.jsonl \\
    --market-id 25 --expected-pos 0

  # Check BNB in batch_place_cancel recording
  PYTHONPATH=/root/myenv/lib/python3.12/site-packages \\
  python adapters/lighter/scripts/lighter_tests/check_bnb_ws_state.py \\
    --jsonl /tmp/nautilus_exec_ws_batch_place_cancel_v2.jsonl \\
    --market-id 25 --expected-pos 0
"""

import argparse
import json
import sys
from collections import defaultdict
from decimal import Decimal
from typing import Any


def _normalize_orders(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten account_all_orders payload into a list of order dicts."""
    orders = msg.get("orders")
    out: list[dict[str, Any]] = []
    if isinstance(orders, list):
        out = [o for o in orders if isinstance(o, dict)]
    elif isinstance(orders, dict):
        for v in orders.values():
            if isinstance(v, list):
                out.extend(o for o in v if isinstance(o, dict))
            elif isinstance(v, dict):
                out.append(v)
    return out


def _normalize_positions(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten account_all_positions payload into a list of position dicts."""
    positions = msg.get("positions")
    out: list[dict[str, Any]] = []
    if isinstance(positions, list):
        out = [p for p in positions if isinstance(p, dict)]
    elif isinstance(positions, dict):
        for v in positions.values():
            if isinstance(v, dict):
                out.append(v)
    return out


def check_ws_state(jsonl_path: str, market_id: int, expected_pos: Decimal) -> None:
    # Per-order status timeline for the target market_id
    status_by_coi: dict[int, list[str]] = defaultdict(list)
    # Last observed position size for the target market_id
    last_pos_size: Decimal | None = None
    saw_position = False

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            msg = rec.get("msg")
            if not isinstance(msg, dict):
                continue
            ch = str(msg.get("channel") or "")
            typ = str(msg.get("type") or "")

            # account_all_orders: collect statuses per client_order_index
            if "account_all_orders:" in ch:
                for od in _normalize_orders(msg):
                    mid = od.get("market_index") or od.get("market_id")
                    try:
                        mid_i = int(str(mid))
                    except Exception:
                        continue
                    if mid_i != market_id:
                        continue
                    coi = od.get("client_order_index")
                    if coi is None:
                        continue
                    try:
                        coi_i = int(str(coi))
                    except Exception:
                        continue
                    status = str(od.get("status") or "")
                    status_by_coi[coi_i].append(status)

            # account_all_positions: track last position size for target market_id
            if "account_all_positions:" in ch:
                for p in _normalize_positions(msg):
                    mid = p.get("market_id") or p.get("market_index")
                    try:
                        mid_i = int(str(mid))
                    except Exception:
                        continue
                    if mid_i != market_id:
                        continue
                    size_raw = p.get("position") or p.get("size")
                    try:
                        last_pos_size = Decimal(str(size_raw))
                        saw_position = True
                    except Exception:
                        continue

    # --- Positions assertions ----------------------------------------------------
    if not saw_position:
        print(f"[FAIL] No account_all_positions entry found for market_id={market_id}")
        sys.exit(1)

    print(f"[BNB-WS] last position size for market_id={market_id}: {last_pos_size}")
    if last_pos_size != expected_pos:
        print(
            f"[FAIL] Expected last position size {expected_pos} for market_id={market_id}, "
            f"got {last_pos_size}"
        )
        sys.exit(1)

    # --- Orders assertions -------------------------------------------------------
    if not status_by_coi:
        print(f"[WARN] No account_all_orders entries found for market_id={market_id}")
    else:
        print(f"[BNB-WS] order status timelines for market_id={market_id}:")
        for coi, statuses in status_by_coi.items():
            print(f"  coi={coi}: {statuses}")
            # At least one 'open'
            if not any(s.lower().startswith("open") for s in statuses):
                print(f"[FAIL] Order coi={coi} has no 'open' status in timeline {statuses}")
                sys.exit(1)
            # If canceled present, check constraints
            canceled_indices = [i for i, s in enumerate(statuses) if s.lower().startswith("canceled")]
            if canceled_indices:
                if len(canceled_indices) != 1:
                    print(f"[FAIL] Order coi={coi} has multiple 'canceled' statuses: {statuses}")
                    sys.exit(1)
                if canceled_indices[0] < max(i for i, s in enumerate(statuses) if s.lower().startswith("open")):
                    print(
                        f"[FAIL] Order coi={coi} has 'canceled' before last 'open' in timeline {statuses}"
                    )
                    sys.exit(1)

    print("[OK] WS BNB state is consistent: positions match expected_pos and order timelines are sane.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True, help="Private WS JSONL file (Execution or record_private_ws)")
    ap.add_argument("--market-id", type=int, default=25, help="Market id to check (default 25 for BNB)")
    ap.add_argument("--expected-pos", type=str, default="0", help="Expected final position size (default 0)")
    args = ap.parse_args()

    try:
        expected_pos = Decimal(args.expected_pos)
    except Exception:
        print(f"[FAIL] Invalid expected-pos value: {args.expected_pos!r}")
        sys.exit(1)

    check_ws_state(args.jsonl, args.market_id, expected_pos)


if __name__ == "__main__":
    main()

