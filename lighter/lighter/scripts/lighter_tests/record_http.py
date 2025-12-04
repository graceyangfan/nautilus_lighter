#!/usr/bin/env python3
from __future__ import annotations

"""
Record Lighter HTTP responses to JSONL using LighterHttpBase's built-in recorder.

This script exercises a small set of HTTP endpoints:
  - /api/v1/orderBooks
  - /api/v1/orderBookDetails?market_id=...
  - /api/v1/account?by=index&value=... (optional, requires auth)
  - /api/v1/accountOrders?by=account_index&value=... (optional, requires auth)

For each response, LighterHttpBase will write a JSONL record when the
environment variable LIGHTER_HTTP_RECORD is set:

  {
    "ts": <nanos>,
    "method": "GET" | "POST",
    "path": "/api/v1/...",
    "params": {...},
    "status": 200,
    "body": <decoded-json-or-text>
  }

Usage (mainnet example):

  export LIGHTER_HTTP_RECORD=/tmp/lighter_http_mainnet.jsonl
  export LIGHTER_PUBLIC_KEY="..."
  export LIGHTER_PRIVATE_KEY="..."
  export LIGHTER_ACCOUNT_INDEX=XXXXX
  export LIGHTER_API_KEY_INDEX=2

  PYTHONPATH=/root/myenv/lib/python3.12/site-packages \\
  python adapters/lighter/scripts/lighter_tests/record_http.py \\
    --http https://mainnet.zklighter.elliot.ai \\
    --account-index XXXXX
"""

import argparse
import asyncio
import os

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.core.nautilus_pyo3 import Quota


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", required=True, help="Base HTTP URL, e.g. https://mainnet.zklighter.elliot.ai")
    ap.add_argument("--account-index", type=int, default=None, help="Account index for private endpoints")
    args = ap.parse_args()

    base_url = args.http.rstrip("/")
    acct_index_env = os.getenv("LIGHTER_ACCOUNT_INDEX")
    acct_index = args.account_index or (int(acct_index_env) if acct_index_env else None)

    record_path = os.getenv("LIGHTER_HTTP_RECORD")
    if not record_path:
        print("WARNING: LIGHTER_HTTP_RECORD not set; no HTTP JSONL will be recorded.")
    else:
        print(f"[HTTP-RECORD] Recording responses to {record_path}")

    # Public client (orderBooks / orderBookDetails)
    quotas = [
        ("lighter:/api/v1/orderBooks", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookDetails", Quota.rate_per_minute(6)),
    ]
    http_pub = LighterPublicHttpClient(base_url=base_url, ratelimiter_quotas=quotas)

    # Fetch order books (summaries)
    print("[HTTP-RECORD] Fetching /api/v1/orderBooks …")
    books = await http_pub.get_order_books()
    print(f"[HTTP-RECORD] orderBooks count={len(books)}")

    # Fetch details for the first market_id (if any)
    if books:
        first_mid = int(books[0].market_id)
        print(f"[HTTP-RECORD] Fetching /api/v1/orderBookDetails?market_id={first_mid} …")
        details = await http_pub.get_order_book_details(first_mid)
        has_details = bool(details)
        print(f"[HTTP-RECORD] orderBookDetails available={has_details}")

    # Private account endpoints (optional)
    if acct_index is None:
        print("[HTTP-RECORD] No account index provided; skipping /api/v1/account and /api/v1/accountOrders.")
        return 0

    pubkey = os.getenv("LIGHTER_PUBLIC_KEY")
    priv = os.getenv("LIGHTER_PRIVATE_KEY")
    api_idx_env = os.getenv("LIGHTER_API_KEY_INDEX", "0")
    if not pubkey or not priv:
        print("[HTTP-RECORD] LIGHTER_PUBLIC_KEY / LIGHTER_PRIVATE_KEY not set; skipping private endpoints.")
        return 0

    creds = LighterCredentials(
        pubkey=pubkey,
        account_index=acct_index,
        api_key_index=int(api_idx_env),
        private_key=priv,
    )
    # We only need the signer to create auth tokens; execution is not used here.
    from nautilus_trader.adapters.lighter.common.signer import LighterSigner
    from nautilus_trader.adapters.lighter.common.auth import LighterAuthManager

    signer = LighterSigner(credentials=creds, base_url=base_url)
    auth_mgr = LighterAuthManager(signer=signer)
    token = auth_mgr.token(horizon_secs=600)

    http_account = LighterAccountHttpClient(base_url=base_url)

    print(f"[HTTP-RECORD] Fetching /api/v1/account for account_index={acct_index} …")
    _ = await http_account.get_account_state(account_index=acct_index, auth=token)

    print(f"[HTTP-RECORD] Fetching /api/v1/accountOrders for account_index={acct_index} …")
    _ = await http_account.get_account_orders(account_index=acct_index, auth=token)

    print("[HTTP-RECORD] Done; inspect LIGHTER_HTTP_RECORD for JSONL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

