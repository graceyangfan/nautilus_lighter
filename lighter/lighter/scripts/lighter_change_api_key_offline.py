#!/usr/bin/env python3
"""
Register (change) Lighter API key on-chain with optional offline signing.

Usage example (Testnet，注意以下均为占位符示例)：

  python scripts/lighter_change_api_key_offline.py \
    --http https://testnet.zklighter.elliot.ai \
    --account-index 0Xxx \
    --api-key-index 0xx \
    --private-key 0x<API_KEY_PRIVATE_KEY_HEX> \
    --new-pubkey 0x<NEW_API_PUBKEY_HEX> \
    --l1-priv-key 0x<YOUR_ETH_L1_PRIVATE_KEY>

If you omit --l1-priv-key, the script prints MessageToSign for offline signing.
You can then rerun with --l1-sig 0x<signature> to submit the transaction.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
import ctypes
import lighter
from lighter.signer_client import StrOrErr as SCStrOrErr


def _to_int(v: str) -> int:
    s = str(v).strip()
    if s.lower().startswith("0x"):
        return int(s, 16)
    return int(s)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", required=True, help="Lighter HTTP base URL (testnet/mainnet)")
    ap.add_argument("--account-index", required=True, help="account index (dec or 0x hex)")
    ap.add_argument("--api-key-index", required=True, help="api key index (dec or 0x hex)")
    ap.add_argument("--private-key", required=True, help="API key private key (hex, with/without 0x)")
    ap.add_argument("--new-pubkey", required=True, help="API key public key to register (0x...) ")
    ap.add_argument("--l1-priv-key", default=None, help="ETH L1 private key for online signing (0x...) ")
    ap.add_argument("--l1-sig", default=None, help="Provide offline 0x signature to submit directly")
    ap.add_argument("--nonce", default="-1", help="nonce (default -1 to auto)")
    args = ap.parse_args()

    url = args.http.rstrip("/")
    account_index = _to_int(args.account_index)
    api_key_index = _to_int(args.api_key_index)
    api_key_priv = args.private_key.strip()
    new_pubkey = args.new_pubkey.strip()
    l1_priv = (args.l1_priv_key or "").strip()
    l1_sig = (args.l1_sig or "").strip()
    nonce = _to_int(args.nonce)

    # Build signer client (also loads the lighter-go library)
    client = lighter.SignerClient(
        url=url,
        private_key=api_key_priv,
        api_key_index=api_key_index,
        account_index=account_index,
    )

    # Access the underlying lighter-go library to prepare change-pubkey payload
    signer_lib = client.signer  # ctypes.CDLL
    # Prototype per lighter-python
    class StrOrErr(SCStrOrErr):
        pass
    signer_lib.SignChangePubKey.argtypes = [
        ctypes.c_char_p,  # new_pubkey
        ctypes.c_longlong,  # nonce
    ]
    signer_lib.SignChangePubKey.restype = StrOrErr

    result = signer_lib.SignChangePubKey(new_pubkey.encode("utf-8"), nonce)
    tx_info_str = result.str.decode("utf-8") if result.str else None
    err = result.err.decode("utf-8") if result.err else None
    if err:
        print(f"SignChangePubKey error: {err}", file=sys.stderr)
        return 3
    if not tx_info_str:
        print("Empty tx_info from SignChangePubKey", file=sys.stderr)
        return 3

    tx_info = json.loads(tx_info_str)
    message = tx_info.get("MessageToSign")
    if not message:
        print("Missing MessageToSign in tx_info", file=sys.stderr)
        return 3

    # Offline path: print message to sign
    if not l1_priv and not l1_sig:
        print("MessageToSign:")
        print(message)
        print("--")
        print("Sign this exact message with the ETH L1 private key of the account and rerun with --l1-sig 0x<signature>.")
        return 0

    # Online sign if L1 private key provided
    if l1_priv and not l1_sig:
        acct = Account.from_key(l1_priv)
        signature = acct.sign_message(encode_defunct(text=message)).signature.to_0x_hex()
        l1_sig = signature

    if not l1_sig.startswith("0x"):
        print("Invalid --l1-sig, must start with 0x", file=sys.stderr)
        return 3

    # Embed signature and submit
    tx_info.pop("MessageToSign", None)
    tx_info["L1Sig"] = l1_sig
    final_tx_info = json.dumps(tx_info)

    tx_api = lighter.TransactionApi(client.api_client)
    resp = await tx_api.send_tx(tx_type=lighter.SignerClient.TX_TYPE_CHANGE_PUB_KEY, tx_info=final_tx_info)
    print("change_api_key sendTx:", getattr(resp, "tx_hash", None))

    # Verify registration
    acc = lighter.AccountApi(client.api_client)
    keys = await acc.apikeys(account_index=account_index, api_key_index=255)
    found = False
    for k in (keys.api_keys or []):
        if str(k.public_key).lower().startswith(new_pubkey.lower().removeprefix("0x")):
            found = True
            print("registered api_key_index:", k.api_key_index)
            break
    print("verify:", "ok" if found else "not_found")

    # Final client check
    err2 = client.check_client()
    print("check_client:", "ok" if not err2 else err2)
    await client.close()
    await client.api_client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
