from __future__ import annotations

"""
Inspect API keys registered on Lighter for an account (no auth required).

Usage:
  python -m scripts.lighter_tests.inspect_apikeys --http https://testnet.zklighter.elliot.ai --account-index 0xx4 [--api-key-index 0]
"""

import argparse
import asyncio
from typing import Any

from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", required=True)
    ap.add_argument("--account-index", required=True)
    ap.add_argument("--api-key-index", default=None)
    args = ap.parse_args()

    def _parse_int(x: str) -> int:
        s = str(x).strip()
        return int(s, 16) if s.startswith("0x") or s.startswith("0X") else int(s)

    acc = _parse_int(str(args.account_index))
    aki = _parse_int(str(args.api_key_index)) if args.api_key_index is not None else None

    http = LighterAccountHttpClient(args.http)
    keys = await http.get_api_keys(account_index=acc, api_key_index=aki)
    if not keys:
        print("No API keys returned")
        return 1
    print("API Keys:")
    for k in keys:
        if not isinstance(k, dict):
            continue
        print({
            "account_index": k.get("account_index"),
            "api_key_index": k.get("api_key_index"),
            "nonce": k.get("nonce"),
            "public_key": k.get("public_key"),
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

