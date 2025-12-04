#!/usr/bin/env python3
from __future__ import annotations

"""
Reduce-Only roundtrip on Lighter testnet:
- Open a tiny position with IOC LIMIT at top-of-book + 1 tick (ensure fill)
- Close the position with IOC LIMIT reduce_only on the opposite side at top-of-book -/+ 1 tick
- Record private WS (account_all + split) to JSONL for analysis

Usage:
  source ~/nautilus_trader-develop/.venv/bin/activate
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python scripts/lighter_tests/reduce_only_roundtrip.py \
    --credentials ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --market-index 1 \
    --notional-usdc 5 \
    --outfile /tmp/lighter_reduce_only.jsonl \
    --runtime 90
"""

import argparse
import asyncio
import json
import math
import time
from typing import Any

import websockets

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.adapters.lighter.http.transaction import LighterTransactionHttpClient
from nautilus_trader.adapters.lighter.common.enums import LighterOrderType, LighterTimeInForce


def _best_px(ob: dict[str, Any]) -> tuple[float | None, float | None]:
    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    def _px(x):
        try:
            return float(x.get("price"))
        except Exception:
            return None
    bid = _px(bids[0]) if bids else None
    ask = _px(asks[0]) if asks else None
    return bid, ask


async def record_ws(ws_url: str, auth: str | None, account: int, outfile: str, run_secs: int) -> None:
    async with websockets.connect(ws_url) as ws:
        subs = [
            {"type": "subscribe", "channel": f"account_all/{account}"},
            {"type": "subscribe", "channel": f"account_all_orders/{account}"},
            {"type": "subscribe", "channel": f"account_all_trades/{account}"},
            {"type": "subscribe", "channel": f"account_all_positions/{account}"},
        ]
        if auth:
            for s in subs:
                s["auth"] = auth
        for s in subs:
            await ws.send(json.dumps(s))
        end = asyncio.get_event_loop().time() + run_secs
        with open(outfile, "a", encoding="utf-8") as f:
            while asyncio.get_event_loop().time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                ch = str(obj.get("channel") or "")
                t = str(obj.get("type") or "")
                if ch.startswith("account_all") or t.startswith("jsonapi/sendtx"):
                    f.write(json.dumps({"ts": int(time.time()*1000), "msg": obj}, ensure_ascii=False) + "\n")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--credentials", required=True)
    ap.add_argument("--http", required=True)
    ap.add_argument("--ws", required=True)
    ap.add_argument("--market-index", type=int, required=True)
    ap.add_argument("--notional-usdc", type=float, default=5.0)
    ap.add_argument("--outfile", default="/tmp/lighter_reduce_only.jsonl")
    ap.add_argument("--runtime", type=int, default=90)
    args = ap.parse_args()

    creds = LighterCredentials.from_json_file(args.credentials)
    signer = LighterSigner(credentials=creds, base_url=args.http, chain_id=300)
    http_pub = LighterPublicHttpClient(args.http)
    http_acc = LighterAccountHttpClient(args.http)
    http_tx = LighterTransactionHttpClient(args.http)
    token = signer.create_auth_token(deadline_seconds=600)

    # Local nonce manager: fetch once, increment locally
    class _LocalNonce:
        def __init__(self, start: int) -> None:
            self._n = int(start)
        def next(self) -> int:
            val = self._n
            self._n += 1
            return val

    # Start recording
    rec_task = asyncio.create_task(record_ws(args.ws, token, creds.account_index, args.outfile, args.runtime))
    await asyncio.sleep(1.0)

    # Decimals + book
    details = await http_pub.get_order_book_details(args.market_index)
    try:
        price_decimals = int(details.price_decimals or 2)
        size_decimals = int(details.size_decimals or 4)
    except AttributeError:
        price_decimals = int(details.get("price_decimals", 2))
        size_decimals = int(details.get("size_decimals", 4))
    scale_px = 10 ** price_decimals
    scale_sz = 10 ** size_decimals
    ob = await http_pub.get_order_book_orders(args.market_index, limit=5)
    best_bid, best_ask = _best_px(ob)
    if best_bid is None and best_ask is None:
        raise SystemExit("No top-of-book; pick an active market")
    px_tick = 10 ** (-price_decimals)

    # Compute open BUY at ask+1 tick to ensure fill
    open_px = (best_ask if best_ask is not None else best_bid) + px_tick
    qty = args.notional_usdc / max(open_px, 1e-9)
    qty = max(qty, 10 ** (-size_decimals))
    qty = math.ceil(qty * scale_sz) / scale_sz
    open_price_i = max(1, int(open_px * scale_px + 1e-9))
    open_size_i = max(1, int(qty * scale_sz + 1e-9))

    # Submit open (IOC BUY)
    base_nonce = await http_acc.get_next_nonce(creds.account_index, creds.api_key_index)
    lnonce = _LocalNonce(base_nonce)
    coi0 = int(time.time_ns() & ((1 << 48) - 1))
    tx0, err0 = await signer.sign_create_order_tx(
        market_index=int(args.market_index),
        client_order_index=int(coi0),
        base_amount=int(open_size_i),
        price=int(open_price_i),
        is_ask=False,
        order_type=LighterOrderType.LIMIT,
        time_in_force=LighterTimeInForce.IOC,
        reduce_only=False,
        trigger_price=0,
        order_expiry=0,
        nonce=int(lnonce.next()),
    )
    if err0:
        print("sign open error:", err0)
    else:
        res0 = await http_tx.send_tx(tx_type=signer.get_tx_type_create_order(), tx_info=tx0, auth=token)
        print("[OPEN] sendTx ->", res0)

    # Short wait then close with reduce_only SELL at bid-1 tick
    await asyncio.sleep(6)
    ob2 = await http_pub.get_order_book_orders(args.market_index, limit=5)
    best_bid2, _ = _best_px(ob2)
    close_px = (best_bid2 if best_bid2 is not None else (best_bid or open_px)) - px_tick
    close_price_i = max(1, int(close_px * scale_px + 1e-9))
    # local increment for close
    coi1 = int((time.time_ns() + 1) & ((1 << 48) - 1))
    tx1, err1 = await signer.sign_create_order_tx(
        market_index=int(args.market_index),
        client_order_index=int(coi1),
        base_amount=int(open_size_i),
        price=int(close_price_i),
        is_ask=True,
        order_type=LighterOrderType.LIMIT,
        time_in_force=LighterTimeInForce.IOC,
        reduce_only=True,
        trigger_price=0,
        order_expiry=0,
        nonce=int(lnonce.next()),
    )
    if err1:
        print("sign close error:", err1)
    else:
        res1 = await http_tx.send_tx(tx_type=signer.get_tx_type_create_order(), tx_info=tx1, auth=token)
        print("[CLOSE] sendTx ->", res1)

    await rec_task
    print("Recorded:", args.outfile)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
