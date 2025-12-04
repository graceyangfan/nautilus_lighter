#!/usr/bin/env python3
from __future__ import annotations

"""
Open a small position, then submit TP/SL reduce-only orders, and record private WS callbacks.

Usage:
  PYTHONPATH=~/.venv/lib/python3.11/site-packages \
  python scripts/lighter_tests/tp_sl_after_open.py \
    --credentials secrets/lighter_testnet_account.json \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --market-index 1 \
    --side long \
    --notional-usdc 10 \
    --trigger-ticks 4 \
    --outfile /tmp/tp_sl_after_open.jsonl \
    --runtime 180
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
    LighterCancelAllTif,
)


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
        loop = asyncio.get_event_loop()
        end = loop.time() + run_secs
        next_ping = loop.time() + 10
        with open(outfile, "a", encoding="utf-8") as f:
            while loop.time() < end:
                now = loop.time()
                if now >= next_ping:
                    try:
                        await ws.send(json.dumps({"type": "ping"}))
                    except Exception:
                        pass
                    next_ping = now + 10
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
                if t == "ping":
                    try:
                        await ws.send(json.dumps({"type": "pong"}))
                    except Exception:
                        pass
                if ch.startswith("account_all") or t.startswith("jsonapi/sendtx"):
                    f.write(json.dumps({"ts": int(time.time()*1000), "msg": obj}, ensure_ascii=False) + "\n")


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


class _LocalNonce:
    def __init__(self, start: int) -> None:
        self._n = int(start)
    def next(self) -> int:
        v = self._n
        self._n += 1
        return v


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--credentials", required=True)
    ap.add_argument("--http", required=True)
    ap.add_argument("--ws", required=True)
    ap.add_argument("--market-index", type=int, required=True)
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--notional-usdc", type=float, default=10.0)
    ap.add_argument("--trigger-ticks", type=int, default=4)
    ap.add_argument("--outfile", default="/tmp/tp_sl_after_open.jsonl")
    ap.add_argument("--runtime", type=int, default=180)
    args = ap.parse_args()

    creds = LighterCredentials.from_json_file(args.credentials)
    signer = LighterSigner(credentials=creds, base_url=args.http, chain_id=300)
    http_pub = LighterPublicHttpClient(args.http)
    http_acc = LighterAccountHttpClient(args.http)
    http_tx = LighterTransactionHttpClient(args.http)
    token = signer.create_auth_token(deadline_seconds=600)

    # Start recorder
    rec_task = asyncio.create_task(record_ws(args.ws, token, creds.account_index, args.outfile, args.runtime))
    await asyncio.sleep(1.0)

    # Decimals and order book
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
    scale_sz = 10 ** size_decimals
    def _i(px: float) -> int:
        return max(1, int(px * scale_px + 1e-9))

    # Compute open order price and size
    side_long = args.side == "long"
    open_px = (best_ask if side_long else best_bid)
    open_px = (open_px if open_px is not None else (best_bid or best_ask or 1000.0)) + (px_tick if side_long else -px_tick)
    qty = max(args.notional_usdc / max(open_px, 1e-9), 10 ** (-size_decimals))
    qty = math.ceil(qty * scale_sz) / scale_sz
    open_price_i = _i(open_px)
    open_size_i = max(1, int(qty * scale_sz + 1e-9))

    # Local nonce manager
    lnonce = _LocalNonce(await http_acc.get_next_nonce(creds.account_index, creds.api_key_index))

    # Open position (IOC)
    coi_open = int(time.time_ns() & ((1 << 48) - 1))
    tx_open, err = await signer.sign_create_order_tx(
        market_index=int(args.market_index),
        client_order_index=int(coi_open),
        base_amount=int(open_size_i),
        price=int(open_price_i),
        is_ask=not side_long,
        order_type=LighterOrderType.LIMIT,
        time_in_force=LighterTimeInForce.IOC,
        reduce_only=False,
        trigger_price=0,
        order_expiry=0,
        nonce=int(lnonce.next()),
    )
    if err:
        print("sign open error:", err)
    else:
        res = await http_tx.send_tx(tx_type=signer.get_tx_type_create_order(), tx_info=tx_open, auth=token)
        print("[OPEN] sendTx ->", res)

    # Short pause to allow position snapshot
    await asyncio.sleep(6)

    # Submit TP/SL reduce-only
    ticks = max(1, int(args.trigger_ticks))
    if side_long:
        tp_trigger = (best_ask if best_ask is not None else (best_bid or open_px)) + ticks * px_tick
        sl_trigger = (best_bid if best_bid is not None else (best_ask or open_px)) - ticks * px_tick
        tp_is_ask = True
        sl_is_ask = True
    else:
        tp_trigger = (best_bid if best_bid is not None else (best_ask or open_px)) - ticks * px_tick
        sl_trigger = (best_ask if best_ask is not None else (best_bid or open_px)) + ticks * px_tick
        tp_is_ask = False
        sl_is_ask = False

    tp_trigger_i = _i(tp_trigger)
    sl_trigger_i = _i(sl_trigger)
    # 使用 limit 变体，price=trigger；可切换到市价变体 price=0
    tp_price_i = tp_trigger_i
    sl_price_i = sl_trigger_i

    def _coi() -> int:
        return int(time.time_ns() & ((1 << 48) - 1))

    # TP
    tx_tp, err_tp = await signer.sign_create_order_tx(
        market_index=int(args.market_index),
        client_order_index=_coi(),
        base_amount=int(open_size_i),
        price=int(tp_price_i),
        is_ask=tp_is_ask,
        order_type=LighterOrderType.TAKE_PROFIT_LIMIT,
        time_in_force=LighterTimeInForce.GTT,
        reduce_only=True,
        trigger_price=int(tp_trigger_i),
        order_expiry=-1,
        nonce=int(lnonce.next()),
    )
    if err_tp:
        print("sign TP error:", err_tp)
    else:
        res_tp = await http_tx.send_tx(tx_type=signer.get_tx_type_create_order(), tx_info=tx_tp, auth=token)
        print("[TP] sendTx ->", res_tp)

    # SL
    tx_sl, err_sl = await signer.sign_create_order_tx(
        market_index=int(args.market_index),
        client_order_index=_coi(),
        base_amount=int(open_size_i),
        price=int(sl_price_i),
        is_ask=sl_is_ask,
        order_type=LighterOrderType.STOP_LOSS_LIMIT,
        time_in_force=LighterTimeInForce.GTT,
        reduce_only=True,
        trigger_price=int(sl_trigger_i),
        order_expiry=-1,
        nonce=int(lnonce.next()),
    )
    if err_sl:
        print("sign SL error:", err_sl)
    else:
        res_sl = await http_tx.send_tx(tx_type=signer.get_tx_type_create_order(), tx_info=tx_sl, auth=token)
        print("[SL] sendTx ->", res_sl)

    await rec_task
    print("Recorded:", args.outfile)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
