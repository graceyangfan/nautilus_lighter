#!/usr/bin/env python3
from __future__ import annotations

"""
Validate recorded Lighter HTTP responses against strict schema definitions.

Input is a JSONL file produced via LIGHTER_HTTP_RECORD. For selected paths,
the script will:

  - Attempt to decode the recorded `body` using msgspec strict structs.
  - Report any decode errors per path.
  - For /api/v1/accountOrders, compare raw order keys vs LighterAccountOrderStrict
    field names to highlight extra/missing fields.
  - For /api/v1/account, inspect `positions` entries vs LighterAccountPositionStrict
    field names.

Usage:

  python adapters/lighter/scripts/lighter_tests/validate_http_schemas.py \\
    --jsonl /tmp/lighter_http_mainnet.jsonl
"""

import argparse
import json
from collections import defaultdict
from typing import Any

import msgspec

from nautilus_trader.adapters.lighter.schemas.http import (
    LighterOrderBooksResponse,
    LighterOrderBookDetailsResponse,
    LighterAccountOrdersResponseStrict,
)
from nautilus_trader.adapters.lighter.schemas.ws import (
    LighterAccountOrderStrict,
    LighterAccountPositionStrict,
)


def _flatten_account_orders_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract a flat list of order dicts from an accountOrders HTTP body."""
    orders = body.get("orders")
    if orders is None and isinstance(body.get("data"), dict):
        orders = body["data"].get("orders")
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


def validate(jsonl_path: str) -> None:
    # Map HTTP path to strict struct type for decode validation
    struct_by_path: dict[str, Any] = {
        "/api/v1/orderBooks": LighterOrderBooksResponse,
        "/api/v1/orderBookDetails": LighterOrderBookDetailsResponse,
        "/api/v1/accountOrders": LighterAccountOrdersResponseStrict,
    }

    # Per-path decode statistics
    ok_counts: dict[str, int] = defaultdict(int)
    err_counts: dict[str, int] = defaultdict(int)
    first_error: dict[str, str] = {}

    # Raw key comparison for accountOrders/account positions
    extra_order_keys: set[str] = set()
    missing_order_keys: set[str] = set()
    extra_pos_keys: set[str] = set()
    missing_pos_keys: set[str] = set()

    # Expected field names from strict structs
    order_fields = set(LighterAccountOrderStrict.__struct_fields__)  # type: ignore[attr-defined]
    pos_fields = set(LighterAccountPositionStrict.__struct_fields__)  # type: ignore[attr-defined]

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            body = rec.get("body")
            if not isinstance(body, dict):
                continue
            path = str(rec.get("path") or "")

            # 1) Strict decode validation for selected paths
            struct_type = struct_by_path.get(path)
            if struct_type is not None:
                try:
                    raw_bytes = msgspec.json.encode(body)
                    msgspec.json.decode(raw_bytes, type=struct_type)
                    ok_counts[path] += 1
                except msgspec.DecodeError as exc:
                    err_counts[path] += 1
                    if path not in first_error:
                        first_error[path] = str(exc)

            # 2) Raw key comparison for /api/v1/accountOrders
            if path == "/api/v1/accountOrders":
                orders = _flatten_account_orders_body(body)
                if orders:
                    sample = orders[0]
                    keys = set(sample.keys())
                    extra_order_keys |= keys - order_fields
                    missing_order_keys |= order_fields - keys

            # 3) Raw key comparison for /api/v1/account (positions)
            if path == "/api/v1/account":
                account = body.get("account")
                if not isinstance(account, dict):
                    accounts = body.get("accounts")
                    if isinstance(accounts, list) and accounts and isinstance(accounts[0], dict):
                        account = accounts[0]
                if isinstance(account, dict):
                    pos_raw = account.get("positions")
                    if isinstance(pos_raw, list) and pos_raw:
                        sample_pos = pos_raw[0]
                        if isinstance(sample_pos, dict):
                            keys = set(sample_pos.keys())
                            extra_pos_keys |= keys - pos_fields
                            missing_pos_keys |= pos_fields - keys

    print(f"[VALIDATE-HTTP] file={jsonl_path}")
    for path in sorted(set(ok_counts.keys()) | set(err_counts.keys())):
        ok = ok_counts.get(path, 0)
        err = err_counts.get(path, 0)
        msg = f"  path={path} decoded_ok={ok} decode_errors={err}"
        if err and path in first_error:
            msg += f" first_error={first_error[path]}"
        print(msg)

    if extra_order_keys or missing_order_keys:
        print("\n[accountOrders] raw order keys vs LighterAccountOrderStrict:")
        if extra_order_keys:
            print(f"  extra_keys_in_payload_not_in_struct={sorted(extra_order_keys)}")
        if missing_order_keys:
            print(f"  missing_keys_in_payload_expected_by_struct={sorted(missing_order_keys)}")

    if extra_pos_keys or missing_pos_keys:
        print("\n[account] raw position keys vs LighterAccountPositionStrict:")
        if extra_pos_keys:
            print(f"  extra_keys_in_payload_not_in_struct={sorted(extra_pos_keys)}")
        if missing_pos_keys:
            print(f"  missing_keys_in_payload_expected_by_struct={sorted(missing_pos_keys)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    args = ap.parse_args()
    validate(args.jsonl)


if __name__ == "__main__":
    main()

