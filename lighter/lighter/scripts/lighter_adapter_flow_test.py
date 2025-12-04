#!/usr/bin/env python3
from __future__ import annotations

"""
End-to-end flow test targeting nautilus_trader/adapters/lighter:

1) 确认账户（nextNonce 即可确认 index 存在）
2) 尝试变更 API Key（使用 lighter-python 官方 `change_api_key`，仅当提供 --l1-priv-key 与 --new-pubkey）
3) 轮询 /api/v1/apikeys 可见性（使用适配器的 LighterAccountHttpClient）
4) 生成 token 并访问受保护接口（使用适配器 HTTP 客户端 + signer.create_auth_token）

示例（Testnet，注意以下均为占位符，不要把真实私钥写入代码）：

  python scripts/lighter_adapter_flow_test.py \
    --http https://testnet.zklighter.elliot.ai \
    --account-index 159 \
    --api-key-index 2 \
    --private-key 0x<API_KEY_PRIVATE_KEY_HEX> \
    --l1-priv-key 0x<ETH_L1_PRIVATE_KEY_HEX> \
    --new-pubkey 0x<NEW_API_PUBKEY_HEX>

说明：第2步（变更 API Key）由 lighter-python 实现；其余步骤均使用适配器组件。
"""

import argparse
import asyncio
import time
from typing import Any

from nautilus_trader.adapters.lighter.common.signer import LighterSigner, LighterCredentials
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.adapters.lighter.http.transaction import LighterTransactionHttpClient


def _to_int(x: str | int) -> int:
    if isinstance(x, int):
        return x
    s = str(x).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s)


async def maybe_change_api_key_with_adapter(base_url: str, account_index: int, api_key_index: int, api_priv: str, l1_priv: str | None, new_pubkey: str | None) -> str | None:
    """Use adapter LighterSigner.change_api_key if L1 and pubkey provided. Return tx_hash or None."""
    if not l1_priv or not new_pubkey:
        return None
    creds = LighterCredentials(pubkey=new_pubkey or "", account_index=account_index, api_key_index=api_key_index, private_key=api_priv)
    signer = LighterSigner(credentials=creds, base_url=base_url)
    resp, err = await signer.change_api_key(eth_private_key=l1_priv, new_pubkey_hex=new_pubkey)
    if err is None and isinstance(resp, dict):
        return str(resp.get("tx_hash") or resp.get("txHash") or "") or None
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", required=True)
    ap.add_argument("--account-index", required=True)
    ap.add_argument("--api-key-index", required=True)
    ap.add_argument("--private-key", required=True, help="API key private key (hex, with/without 0x)")
    ap.add_argument("--l1-priv-key", default=None, help="ETH L1 private key (0x...) for change_api_key")
    ap.add_argument("--new-pubkey", default=None, help="API key public key (0x...) for change_api_key")
    args = ap.parse_args()

    base_url = args.http.rstrip("/")
    account_index = _to_int(args.account_index)
    api_key_index = _to_int(args.api_key_index)
    api_priv = args.private_key.strip()
    l1_priv = (args.l1_priv_key or "").strip() or None
    new_pubkey = (args.new_pubkey or "").strip() or None

    # 1) 确认账户（通过 nextNonce 验证 account_index + api_key_index 可访问）
    acc = LighterAccountHttpClient(base_url)
    nonce = await acc.get_next_nonce(account_index=account_index, api_key_index=api_key_index)
    print(f"nextNonce({account_index},{api_key_index})=", nonce)

    # 2) 尝试变更 API Key（仅当提供 L1 私钥与新公钥）
    txh = await maybe_change_api_key_with_adapter(base_url, account_index, api_key_index, api_priv, l1_priv, new_pubkey)
    if txh:
        print("change_api_key tx_hash:", txh)

    # 3) 轮询 apikeys 可见性
    if new_pubkey:
        target = new_pubkey.lower().removeprefix("0x")
        found = False
        for i in range(20):
            keys = await acc.get_api_keys(account_index=account_index, api_key_index=None)
            has = any((k.get("public_key") or "").lower().startswith(target) for k in keys)
            print(f"poll {i}: apikeys count={len(keys)} found={has}")
            if has:
                found = True
                break
            time.sleep(3)
        if not found:
            print("warning: api key not visible yet; continuing to auth step…")

    # 4) 生成 token 并访问受保护接口
    creds = LighterCredentials(pubkey=new_pubkey or "", account_index=account_index, api_key_index=api_key_index, private_key=api_priv)
    signer = LighterSigner(credentials=creds, base_url=base_url)
    token = signer.create_auth_token(deadline_seconds=600)
    print("token_len:", len(token or ""))
    txs = await acc.get_account_txs(account_index=account_index, index=0, limit=1, auth=token)
    print("account_txs keys:", list(txs.keys())[:5] if isinstance(txs, dict) else type(txs))

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
