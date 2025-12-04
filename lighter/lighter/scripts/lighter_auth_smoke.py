#!/usr/bin/env python3
from __future__ import annotations

"""
Lighter auth smoke test using lighter-python.

- Verifies public GET, signer availability, nextNonce, auth token creation,
  and a protected endpoint call (account_txs) with both header + query auth.

Usage (Testnet):

  python scripts/lighter_auth_smoke.py \
    --http https://testnet.zklighter.elliot.ai \
    --account-index 0xxx \
    --api-key-index 0xx \
    --private-key xxxxxx...xxxx
"""

import argparse
import asyncio
import json
from typing import Any

import lighter


def _to_int(v: Any) -> int:
    if isinstance(v, int):
        return v
    s = str(v).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", required=True)
    ap.add_argument("--account-index", required=True)
    ap.add_argument("--api-key-index", required=True)
    ap.add_argument("--private-key", required=True)
    args = ap.parse_args()

    base_url = args.http.rstrip("/")
    account_index = _to_int(args.account_index)
    api_key_index = _to_int(args.api_key_index)
    priv = args.private_key.strip()

    # 1) public
    api_client = lighter.ApiClient(configuration=lighter.Configuration(host=base_url))
    orders = await lighter.OrderApi(api_client).order_books()
    print("orderBooks ok:", bool(getattr(orders, "order_books", None)))

    # 2) signer
    signer = lighter.SignerClient(
        url=base_url,
        private_key=priv,
        api_key_index=api_key_index,
        account_index=account_index,
    )
    err = signer.check_client()
    print("check_client:", "ok" if err is None else err)

    # 3) nonce
    next_nonce = await lighter.TransactionApi(api_client).next_nonce(
        account_index=account_index, api_key_index=api_key_index
    )
    print("nextNonce:", getattr(next_nonce, "nonce", None))

    # 4) auth token
    token, terr = signer.create_auth_token_with_expiry(10 * 60)
    print("auth token len:", 0 if terr or not token else len(token))
    if terr:
        print("create_auth_token error:", terr)

    # 5) protected call (header + query)
    try:
        txs = await lighter.TransactionApi(api_client).account_txs(
            limit=1,
            by="account_index",
            value=str(account_index),
            authorization=token,
            auth=token,
        )
        print("account_txs ok:", True)
    except Exception as e:
        print("account_txs error:", e)

    await signer.close()
    await api_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

