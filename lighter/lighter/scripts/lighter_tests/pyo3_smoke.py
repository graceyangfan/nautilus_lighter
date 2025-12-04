#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.adapters.lighter.http.transaction import LighterTransactionHttpClient
from nautilus_trader.adapters.lighter.http.errors import LighterHttpError


def load_creds(path: Path) -> LighterCredentials:
    data = json.loads(path.read_text(encoding="utf-8"))
    def _to_int(v: Any) -> int:
        if isinstance(v, int):
            return v
        s = str(v)
        return int(s, 16) if s.startswith("0x") else int(s)
    return LighterCredentials(
        pubkey=str(data["pubkey"]),
        account_index=_to_int(data["account_index"]),
        api_key_index=_to_int(data["api_key_index"]),
        nonce=_to_int(data["nonce"]) if data.get("nonce") is not None else None,
        private_key=(data.get("private_key") or "").strip(),
    )


async def main() -> None:
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--credentials", type=Path, help="path to secrets JSON (optional if env vars set)")
    ap.add_argument("--http", type=str, default="https://mainnet.zklighter.elliot.ai")
    args = ap.parse_args()

    base_url = args.http.rstrip("/")

    # Load from env vars or file
    if args.credentials:
        creds = load_creds(args.credentials)
    else:
        pubkey = os.getenv("LIGHTER_PUBLIC_KEY")
        private_key = os.getenv("LIGHTER_PRIVATE_KEY")
        account_index = os.getenv("LIGHTER_ACCOUNT_INDEX")
        if not all([pubkey, private_key, account_index]):
            print("ERROR: Need --credentials or env vars (LIGHTER_PUBLIC_KEY, LIGHTER_PRIVATE_KEY, LIGHTER_ACCOUNT_INDEX)")
            return
        creds = LighterCredentials(
            pubkey=pubkey,
            account_index=int(account_index),
            api_key_index=0,
            private_key=private_key,
        )

    # 1) public
    pub = LighterPublicHttpClient(base_url)
    books = await pub.get_order_books()
    print(f"orderBooks ok: {len(books)}")

    # 2) signer
    signer = LighterSigner(creds, base_url=base_url)
    print("signer available:", signer.available())
    token = signer.create_auth_token(deadline_seconds=60)
    print("auth token length:", len(token))

    # 3) account
    acc = LighterAccountHttpClient(base_url)
    nn = await acc.get_next_nonce(account_index=creds.account_index, api_key_index=creds.api_key_index)
    print("nextNonce:", nn)
    state = await acc.get_account_state(account_index=creds.account_index, auth=token)
    print("account state keys:", list(state.keys())[:5])

    # 4) transaction (intentionally invalid payload to check error plumbing)
    tx = LighterTransactionHttpClient(base_url)
    try:
        await tx.send_tx(tx_type=9999, tx_info="{}", price_protection=False, auth=token)
        print("sendTx unexpected ok")
    except LighterHttpError as e:
        print("sendTx error as expected:", e.status)


if __name__ == "__main__":
    asyncio.run(main())

