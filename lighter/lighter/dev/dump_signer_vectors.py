from __future__ import annotations

"""
Dump lighter-go signer tx_info vectors for quick A/B comparison.

Generates signed payloads for CREATE_ORDER / CANCEL_ORDER / CANCEL_ALL to
help compare against lighter-python outputs and triage signature issues.

Examples (run outside repo root):
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python -m nautilus_trader.adapters.lighter.dev.dump_signer_vectors \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --http https://testnet.zklighter.elliot.ai \
    --op create --market-id 1 --side buy --notional 10 --ticks 1

  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python -m nautilus_trader.adapters.lighter.dev.dump_signer_vectors \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --http https://testnet.zklighter.elliot.ai \
    --op cancel_all
"""

import argparse
import asyncio
import json
import math
from typing import Any

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner
from nautilus_trader.adapters.lighter.common.enums import (
    LighterOrderType,
    LighterTimeInForce,
    LighterCancelAllTif,
)
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient


def _print(title: str, obj: Any) -> None:
    try:
        print(title, json.dumps(obj, ensure_ascii=False))
    except Exception:
        print(title, obj)


async def _compute_scaled_inputs(http_pub: LighterPublicHttpClient, market_id: int, side: str, ticks: int, notional: float, price_decimals: int | None = None, size_decimals: int | None = None, min_base_amount: float | None = None) -> tuple[int, int, dict[str, Any]]:
    det = await http_pub.get_order_book_details(market_id)
    p_dec = int(det.get("price_decimals", 1) if price_decimals is None else price_decimals)
    s_dec = int(det.get("size_decimals", 4) if size_decimals is None else size_decimals)
    # Prefer HTTP summaries for min_base_amount; fallback to details
    if min_base_amount is None:
        summaries = await http_pub.get_order_books()
        mba = 0.0
        for s in summaries:
            try:
                if int(s.get("market_id")) == int(market_id):
                    mba = float(s.get("min_base_amount") or 0.0)
                    break
            except Exception:
                continue
        if mba <= 0:
            mba = float(det.get("min_base_amount") or 0.0) or 0.001
    else:
        mba = float(min_base_amount)
    # Get top of book
    try:
        ob = await http_pub.get_order_book_orders(market_id, limit=5)
        def first_px(arr):
            if not isinstance(arr, list) or not arr:
                return None
            x = arr[0].get("price")
            return float(x) if x is not None else None
        best_bid, best_ask = first_px(ob.get("bids")), first_px(ob.get("asks"))
    except Exception:
        best_bid, best_ask = None, None
    if best_bid is None and best_ask is None:
        best_bid, best_ask = 2000.0, 2010.0
    tick = 10 ** (-p_dec)
    side_l = str(side or "").lower()
    ticks = max(0, int(ticks))
    if side_l == "buy":
        target = (best_bid or 0.0) + ticks * tick
        if best_ask is not None:
            target = min(target, best_ask - tick)
    else:
        target = (best_ask or 0.0) - ticks * tick
        if best_bid is not None:
            target = max(target, best_bid + tick)
    scale_px = 10 ** p_dec
    scale_sz = 10 ** s_dec
    bid_i = int((best_bid or 0.0) * scale_px)
    ask_i = int((best_ask or 0.0) * scale_px)
    tgt_i = int(target * scale_px)
    if side_l == "buy" and best_ask is not None:
        tgt_i = min(tgt_i, ask_i - 1)
    if side_l == "sell" and best_bid is not None:
        tgt_i = max(tgt_i, bid_i + 1)
    # qty from notional and min
    qty_f = max(float(notional) / max(target, 1e-9), float(mba))
    step = 10 ** (-s_dec)
    qty_f = math.ceil(qty_f / step) * step
    qty_i = max(1, int(qty_f * scale_sz + 1e-9))
    meta = {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "price_decimals": p_dec,
        "size_decimals": s_dec,
        "min_base_amount": mba,
        "target_price": target,
        "target_price_int": tgt_i,
        "target_qty": qty_f,
        "target_qty_int": qty_i,
    }
    return tgt_i, qty_i, meta


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secrets", required=True)
    ap.add_argument("--http", required=True)
    ap.add_argument("--op", choices=["create", "cancel", "cancel_all"], default="create")
    ap.add_argument("--market-id", type=int, default=0)
    ap.add_argument("--side", choices=["buy", "sell"], default="buy")
    ap.add_argument("--ticks", type=int, default=1)
    ap.add_argument("--notional", type=float, default=10.0)
    ap.add_argument("--order-index", type=int, default=None, help="venue order_index for cancel")
    ap.add_argument("--chain-id", type=int, default=300)
    ap.add_argument("--print-raw", action="store_true", help="print raw tx_info JSON string")
    args = ap.parse_args()

    creds = LighterCredentials.from_json_file(args.secrets)
    signer = LighterSigner(credentials=creds, base_url=args.http, chain_id=int(args.chain_id))
    http_pub = LighterPublicHttpClient(args.http)
    http_acc = LighterAccountHttpClient(args.http)

    n = await http_acc.get_next_nonce(creds.account_index, creds.api_key_index)
    token = signer.create_auth_token()

    if args.op == "create":
        price_i, base_i, meta = await _compute_scaled_inputs(http_pub, int(args.market_id), str(args.side), int(args.ticks), float(args.notional))
        coi = (n & ((1 << 48) - 1))
        tx_info, err = await signer.sign_create_order_tx(
            market_index=int(args.market_id),
            client_order_index=int(coi),
            base_amount=int(base_i),
            price=int(price_i),
            is_ask=(str(args.side).lower() == "sell"),
            order_type=LighterOrderType.LIMIT,
            time_in_force=LighterTimeInForce.GTT,
            reduce_only=False,
            trigger_price=0,
            order_expiry=-1,
            nonce=int(n),
        )
        if err or not tx_info:
            print("sign error:", err)
            return 2
        obj = json.loads(tx_info)
        # redact Sig
        red = {k: ("<redacted>" if k.lower() == "sig" else obj.get(k)) for k in sorted(obj.keys())}
        _print("inputs:", {
            "market_index": int(args.market_id),
            "client_order_index": int(coi),
            "base_amount": int(base_i),
            "price": int(price_i),
            "is_ask": (str(args.side).lower() == "sell"),
            "order_type": int(LighterOrderType.LIMIT),
            "time_in_force": int(LighterTimeInForce.GTT),
            "reduce_only": False,
            "trigger_price": 0,
            "order_expiry": -1,
            "nonce": int(n),
        })
        _print("scaled_meta:", meta)
        _print("tx_info:", red)
        if args.print_raw:
            print("tx_info_raw:", tx_info)
        return 0

    if args.op == "cancel_all":
        tx_info, err = await signer.sign_cancel_all_orders_tx(time_in_force=LighterCancelAllTif.IMMEDIATE, time=0, nonce=int(n))
        if err or not tx_info:
            print("sign error:", err)
            return 2
        obj = json.loads(tx_info)
        red = {k: ("<redacted>" if k.lower() == "sig" else obj.get(k)) for k in sorted(obj.keys())}
        _print("tx_info:", red)
        if args.print_raw:
            print("tx_info_raw:", tx_info)
        return 0

    if args.op == "cancel":
        if args.order_index is None:
            print("--order-index required for --op cancel")
            return 2
        tx_info, err = await signer.sign_cancel_order_tx(market_index=int(args.market_id), order_index=int(args.order_index), nonce=int(n))
        if err or not tx_info:
            print("sign error:", err)
            return 2
        obj = json.loads(tx_info)
        red = {k: ("<redacted>" if k.lower() == "sig" else obj.get(k)) for k in sorted(obj.keys())}
        _print("tx_info:", red)
        if args.print_raw:
            print("tx_info_raw:", tx_info)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

