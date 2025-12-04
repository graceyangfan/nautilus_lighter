#!/usr/bin/env python3
"""
Lightweight tests for nautilus_trader.adapters.lighter.common components.

This script validates:
- LighterCredentials loading from JSON (secrets/lighter_testnet_account.json)
- NonceManager behaviour (ensure/next/rollback_and_refresh)
- LighterSigner initialization path (will raise if signer library is missing)

It does not place real orders. If the platform signer library is available
under adapters/lighter/signers/, the signer "available" check will pass and
create_auth_token() may return a non-empty token depending on venue state.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pathlib import Path
import sys as _sys

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / ".venv" / "lib" / "python3.11" / "site-packages"
if SITE.exists():
    _sys.path.insert(0, str(SITE))

from nautilus_trader.adapters.lighter.common.nonce import NonceManager
from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner


def test_credentials(secrets_path: Path) -> LighterCredentials:
    data = json.loads(secrets_path.read_text(encoding="utf-8"))
    creds = LighterCredentials.from_json_file(secrets_path)
    assert isinstance(creds.pubkey, str) and creds.pubkey.startswith("0x"), "pubkey must be hex string"
    assert isinstance(creds.account_index, int)
    assert isinstance(creds.api_key_index, int)
    assert isinstance(creds.nonce, (int, type(None)))
    assert isinstance(creds.private_key, (str, type(None)))
    # Cross-check fields are populated as expected
    assert creds.account_index == int(str(data["account_index"]), 16)
    assert creds.api_key_index == int(str(data["api_key_index"]), 16)
    # Validate private key format: accept 64-hex (32 bytes) or 80-hex (40 bytes), no 0x prefix
    pk = (creds.private_key or "").lower()
    if pk.startswith("0x"):
        pk = pk[2:]
    assert len(pk) in (64, 80) and all(c in "0123456789abcdef" for c in pk), "private_key must be 64 or 80 hex chars (no 0x)"
    print("[OK] LighterCredentials loaded and validated")
    return creds


async def test_nonce_manager(initial: int) -> None:
    nm = NonceManager()

    async def fetcher(_: int) -> int:
        return initial

    # Ensure + first next
    v0 = await nm.next(0, fetcher)
    assert v0 == initial, f"expected {initial} got {v0}"
    # Next increments
    v1 = await nm.next(0, fetcher)
    assert v1 == initial + 1
    # Rollback and refresh
    v2 = await nm.rollback_and_refresh(0, fetcher)
    assert v2 == initial
    print("[OK] NonceManager ensure/next/rollback validated")


def test_signer_init(creds: LighterCredentials, base_url: str, chain_id: int | None) -> None:
    import sys as _sys
    # Prefer new signature with chain_id, fallback for older installed versions
    try:
        try:
            signer = LighterSigner(credentials=creds, base_url=base_url, chain_id=chain_id)
        except TypeError:
            signer = LighterSigner(credentials=creds, base_url=base_url)  # type: ignore[call-arg]
    except RuntimeError as e:
        # Likely missing local signer library
        print("[SKIP] LighterSigner init (library missing):", str(e))
        return

    # Quick environment summary (sanitized)
    pk = (creds.private_key or "")
    pk_clean = pk[2:] if pk.lower().startswith("0x") else pk
    pk_len = len(pk_clean)
    pk_hex_ok = all(c in "0123456789abcdefABCDEF" for c in pk_clean)
    chain = chain_id if chain_id is not None else (304 if "mainnet" in base_url.lower() else 300)

    try:
        assert signer.available(), "signer should report available once library is loaded"
        tok = signer.create_auth_token()
        print("[OK] LighterSigner available; auth token length=", len(tok or ""))
    except RuntimeError as e:
        # Surface go-side CreateClient error and helpful context for debugging
        print("[ERR] CreateClient/auth_token error:", str(e))
        print("[CTX] base_url=", base_url)
        print("[CTX] chain_id=", chain)
        print("[CTX] account_index=", creds.account_index, " api_key_index=", creds.api_key_index)
        print("[CTX] private_key length=", pk_len, " hex_ok=", pk_hex_ok, " has_0x_prefix=", pk.lower().startswith("0x"))
        _sys.exit(3)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secrets", default="secrets/lighter_testnet_account.json")
    ap.add_argument("--http", default="https://testnet.zklighter.elliot.ai")
    ap.add_argument("--chain-id", type=int, default=None)
    args = ap.parse_args()

    secrets_path = Path(args.secrets).resolve()
    if not secrets_path.exists():
        print("[ERR] secrets file not found:", secrets_path)
        sys.exit(2)

    creds = test_credentials(secrets_path)

    # NonceManager test (use provided nonce or default 0)
    initial = int(creds.nonce or 0)
    import asyncio

    asyncio.run(test_nonce_manager(initial))

    # Signer init (will skip if library not present)
    test_signer_init(creds, args.http, args.chain_id)
    print("[DONE] lighter.common tests")


if __name__ == "__main__":
    main()
