from __future__ import annotations

"""
Verify Lighter testnet secrets using lighter-python.

This script helps ensure that the secret file matches the expectations of
lighter-python: the `private_key` must be the API Key private key (not the
wallet private key). It calls `check_client()` which compares the derived
public key from `private_key` with the server-registered API key public key
for the given `account_index` and `api_key_index`.

Usage:
  python -m scripts.lighter_tests.verify_secrets_with_lighter_python \
    --secrets secrets/lighter_testnet_account.json \
    --http https://testnet.zklighter.elliot.ai

Note:
  - Requires `lighter` package installed (pip install git+https://github.com/elliottech/lighter-python.git)
  - Run this outside repo root or set PYTHONPATH to your site-packages if needed.
"""

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secrets", required=True)
    ap.add_argument("--http", required=True)
    args = ap.parse_args()

    data = json.loads(Path(args.secrets).read_text(encoding="utf-8"))
    pubkey = str(data.get("pubkey"))
    account_index = data.get("account_index")
    api_key_index = data.get("api_key_index")
    priv = str(data.get("private_key") or "")

    # normalize
    try:
        if isinstance(account_index, str) and account_index.startswith("0x"):
            account_index_int = int(account_index, 16)
        else:
            account_index_int = int(account_index)
    except Exception:
        print("invalid account_index in secrets")
        return 2
    try:
        if isinstance(api_key_index, str) and api_key_index.startswith("0x"):
            api_key_index_int = int(api_key_index, 16)
        else:
            api_key_index_int = int(api_key_index)
    except Exception:
        print("invalid api_key_index in secrets")
        return 2
    if not priv:
        print("missing private_key in secrets (must be API Key private key)")
        return 2

    # Defer to lighter-python for authoritative check
    try:
        import lighter
        from lighter import SignerClient
    except Exception as e:
        print("lighter-python is not installed:", e)
        print("Install: pip install git+https://github.com/elliottech/lighter-python.git")
        return 2

    client = SignerClient(url=args.http, private_key=priv, account_index=account_index_int, api_key_index=api_key_index_int)
    err = client.check_client()
    if err is None:
        print("OK: secrets match server API key (check_client passed)")
        # Also try auth token generation
        auth, err_auth = client.create_auth_token_with_expiry()
        if auth and not err_auth:
            print("Auth token generated; length=", len(auth))
        else:
            print("WARN: create_auth_token reported error:", err_auth)
        return 0
    # Print the last line of error for brevity
    msg = str(err).strip().split("\n")[-1]
    print("ERROR:", msg)
    print("Hint: The `private_key` must be the API Key private key, not the wallet private key.")
    print("      Ensure `api_key_index` matches the key produced in the Lighter console.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

