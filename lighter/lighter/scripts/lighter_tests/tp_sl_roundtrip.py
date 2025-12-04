#!/usr/bin/env python3
from __future__ import annotations

"""
Submit TP/SL orders (STOP/IF_TOUCHED, LIMIT/MARKET variants) on Lighter testnet,
optionally with reduce_only, and record private WS (account_all + split) to JSONL.

Usage (example):
  source ~/nautilus_trader-develop/.venv/bin/activate
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python scripts/lighter_tests/tp_sl_roundtrip.py \
    --credentials ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --market-index 1 \
    --side long \
    --tp-kind limit \
    --sl-kind limit \
    --reduce-only \
    --outfile /tmp/lighter_tp_sl.jsonl \
    --runtime 120

Notes
-----
- This script aims to verify acceptance + callbacks; actual trigger depends on
  market price movement. You can cancel untriggered orders with --cancel-after.
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
from nautilus_trader.adapters.lighter.common.enums import (
    LighterOrderType,
    LighterTimeInForce,
)


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
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--tp-kind", choices=["none", "limit", "market"], default="limit", help="take profit kind")
    ap.add_argument("--sl-kind", choices=["none", "limit", "market"], default="limit", help="stop loss kind")
    ap.add_argument("--reduce-only", action="store_true")
    ap.add_argument("--outfile", default="/tmp/lighter_tp_sl.jsonl")
    ap.add_argument("--runtime", type=int, default=120)
    ap.add_argument("--cancel-after", type=int, default=None, help="seconds to wait then cancel all TP/SL (optional)")
    ap.add_argument("--trigger-ticks", type=int, default=2, help="distance from top-of-book for triggers")
    args = ap.parse_args()

    creds = LighterCredentials.from_json_file(args.credentials)
    signer = LighterSigner(credentials=creds, base_url=args.http, chain_id=300)
    http_pub = LighterPublicHttpClient(args.http)
    http_acc = LighterAccountHttpClient(args.http)
    http_tx = LighterTransactionHttpClient(args.http)
    token = signer.create_auth_token(deadline_seconds=600)

    # Fetch decimals + top-of-book
    details = await http_pub.get_order_book_details(args.market_index)
    try:
        price_decimals = int(details.price_decimals or 2)
        size_decimals = int(details.size_decimals or 4)
    except AttributeError:
        price_decimals = int(details.get("price_decimals", 2))
        size_decimals = int(details.get("size_decimals", 4))
    ob = await http_pub.get_order_book_orders(args.market_index, limit=5)
    best_bid, best_ask = _best_px(ob)
    if best_bid is None and best_ask is None:
        raise SystemExit("No top-of-book; pick an active market")

    px_tick = 10 ** (-price_decimals)
    scale_px = 10 ** price_decimals
    # Helper to scale float -> venue int
    def _to_px_i(px: float) -> int:
        return max(1, int(px * scale_px + 1e-9))
    # Determine a minimal base_amount integer, based on market summary
    summaries = await http_pub.get_order_books()
    min_base = 0.0
    try:
        for s in summaries:
            if int(s.get("market_id")) == int(args.market_index):
                mb = s.get("min_base_amount")
                min_base = float(mb) if mb is not None else 0.0
                break
    except Exception:
        min_base = 0.0
    scale_sz = 10 ** size_decimals
    base_amount_i = max(1, int(math.ceil((min_base or (1.0 / scale_sz)) * scale_sz)))

    # Compute trigger/limit anchors relative to book
    ticks = max(1, int(args.trigger_ticks))
    side_long = args.side == "long"

    # TP setup
    tp_kind = args.tp_kind.lower()
    tp_type = None
    tp_price_i = 0
    tp_trigger_i = 0
    if tp_kind != "none":
        if side_long:
            # Long take-profit triggers above current ask
            tp_trigger = (best_ask if best_ask is not None else best_bid) + ticks * px_tick
            tp_price = 0.0 if tp_kind == "market" else tp_trigger
        else:
            # Short take-profit triggers below current bid
            tp_trigger = (best_bid if best_bid is not None else best_ask) - ticks * px_tick
            tp_price = 0.0 if tp_kind == "market" else tp_trigger
        tp_trigger_i = _to_px_i(tp_trigger)
        tp_price_i = _to_px_i(tp_price)
        tp_type = (LighterOrderType.TAKE_PROFIT if tp_kind == "market" else LighterOrderType.TAKE_PROFIT_LIMIT)

    # SL setup
    sl_kind = args.sl_kind.lower()
    sl_type = None
    sl_price_i = 0
    sl_trigger_i = 0
    if sl_kind != "none":
        if side_long:
            # Long stop-loss triggers below current bid
            sl_trigger = (best_bid if best_bid is not None else best_ask) - ticks * px_tick
            sl_price = 0.0 if sl_kind == "market" else sl_trigger
        else:
            # Short stop-loss triggers above current ask
            sl_trigger = (best_ask if best_ask is not None else best_bid) + ticks * px_tick
            sl_price = 0.0 if sl_kind == "market" else sl_trigger
        sl_trigger_i = _to_px_i(sl_trigger)
        sl_price_i = _to_px_i(sl_price)
        sl_type = (LighterOrderType.STOP_LOSS if sl_kind == "market" else LighterOrderType.STOP_LOSS_LIMIT)

    # Local nonce manager to reduce /nextNonce calls
    class _LocalNonce:
        def __init__(self, start: int) -> None:
            self._n = int(start)
        def next(self) -> int:
            v = self._n
            self._n += 1
            return v
    lnonce = _LocalNonce(await http_acc.get_next_nonce(creds.account_index, creds.api_key_index))

    # Submit TP/SL (each as independent order)
    async def _submit(order_type: LighterOrderType, price_i: int, trigger_i: int) -> dict:
        nonce = lnonce.next()
        coi = int(time.time_ns() & ((1 << 48) - 1))
        is_ask = (not side_long) if order_type in (LighterOrderType.TAKE_PROFIT, LighterOrderType.TAKE_PROFIT_LIMIT) else (side_long)
        # TP orders close LONG with SELL, STOP orders close LONG with SELL; for SHORT 反向
        tx, err = await signer.sign_create_order_tx(
            market_index=int(args.market_index),
            client_order_index=int(coi),
            base_amount=base_amount_i,
            price=price_i,
            is_ask=is_ask,
            order_type=order_type,
            time_in_force=LighterTimeInForce.GTT,
            reduce_only=bool(args.reduce_only),
            trigger_price=trigger_i,
            order_expiry=-1,
            nonce=int(nonce),
        )
        if err:
            return {"error": str(err)}
        return await http_tx.send_tx(tx_type=signer.get_tx_type_create_order(), tx_info=tx, auth=token)

    # Run recorder + submissions concurrently
    tasks = []
    tasks.append(asyncio.create_task(record_ws(args.ws, token, creds.account_index, args.outfile, args.runtime)))
    # Small delay to ensure subscriptions in place
    await asyncio.sleep(1.0)

    results: list[tuple[str, dict]] = []
    if tp_type is not None:
        res = await _submit(tp_type, tp_price_i, tp_trigger_i)
        results.append(("TP", res))
    if sl_type is not None:
        res = await _submit(sl_type, sl_price_i, sl_trigger_i)
        results.append(("SL", res))
    for tag, res in results:
        print(f"[{tag}] sendTx ->", res)

    # Optional cancel late (best-effort cancel-all)
    if args.cancel_after is not None and args.cancel_after > 0:
        await asyncio.sleep(float(args.cancel_after))
        nonce = lnonce.next()
        # Venue Cancel-All TIF IMMEDIATE; send via WS jsonapi or REST – choose REST for determinism
        from nautilus_trader.adapters.lighter.common.enums import LighterCancelAllTif
        tx_all, err = await signer.sign_cancel_all_orders_tx(LighterCancelAllTif.IMMEDIATE, 0, int(nonce))
        if not err and tx_all:
            res_c = await http_tx.send_tx(tx_type=signer.get_tx_type_cancel_all(), tx_info=tx_all, auth=token)
            print("[CANCEL_ALL] ->", res_c)

    await asyncio.gather(*tasks)
    print("Done. Recorded:", args.outfile)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
