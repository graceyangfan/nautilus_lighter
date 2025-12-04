#!/usr/bin/env python3
from __future__ import annotations

"""
Check BNB (market_id=25) account state in recorded HTTP JSONL.

This script inspects a JSONL file produced via LIGHTER_HTTP_RECORD, locates
the latest `/api/v1/account` snapshot, and verifies that:

  - There is a position entry for market_id=25 (BNB).
  - The symbol is "BNB".
  - The position size is 0 (flat) for this snapshot.

This is useful as a concrete, business-level assertion for the BNB market,
on top of the generic schema validation.

Usage:

  PYTHONPATH=/root/myenv/lib/python3.12/site-packages \\
  python adapters/lighter/scripts/lighter_tests/check_bnb_account_state_http.py \\
    --jsonl /tmp/lighter_http_mainnet.jsonl \\
    --market-id 25
"""

import argparse
import json
import sys
from decimal import Decimal
from typing import Any


def _find_latest_account_with_position(path: str, market_id: int) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("path") != "/api/v1/account":
                continue
            body = rec.get("body")
            if not isinstance(body, dict):
                continue
            account = body.get("account")
            if not isinstance(account, dict):
                accounts = body.get("accounts")
                if isinstance(accounts, list) and accounts and isinstance(accounts[0], dict):
                    account = accounts[0]
            if not isinstance(account, dict):
                continue
            positions = account.get("positions")
            if not isinstance(positions, list):
                continue
            for p in positions:
                if not isinstance(p, dict):
                    continue
                mid = p.get("market_id") or p.get("market_index")
                try:
                    mid_i = int(str(mid))
                except Exception:
                    continue
                if mid_i == market_id:
                    latest = p
    return latest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True, help="HTTP JSONL file recorded via LIGHTER_HTTP_RECORD")
    ap.add_argument("--market-id", type=int, default=25, help="Market id to check (default 25 for BNB)")
    args = ap.parse_args()

    pos = _find_latest_account_with_position(args.jsonl, args.market_id)
    if pos is None:
        print(f"[FAIL] No position entry found for market_id={args.market_id} in /api/v1/account snapshots")
        sys.exit(1)

    symbol = str(pos.get("symbol") or "")
    position_str = str(pos.get("position") or pos.get("size") or "0")
    imf = str(pos.get("initial_margin_fraction") or "")

    print(f"[BNB-ACCOUNT] market_id={args.market_id} symbol={symbol} "
          f"position={position_str} initial_margin_fraction={imf}")

    # Basic assertions for the BNB snapshot:
    if symbol.upper() != "BNB":
        print(f"[FAIL] Expected symbol BNB for market_id={args.market_id}, got {symbol!r}")
        sys.exit(1)

    try:
        pos_dec = Decimal(position_str)
    except Exception:
        print(f"[FAIL] position field not numeric: {position_str!r}")
        sys.exit(1)

    if pos_dec != Decimal("0"):
        print(f"[FAIL] Expected flat BNB position (0) for market_id={args.market_id}, got {pos_dec}")
        sys.exit(1)

    print("[OK] BNB account position is flat and symbol matches BNB.")
    sys.exit(0)


if __name__ == "__main__":
    main()

