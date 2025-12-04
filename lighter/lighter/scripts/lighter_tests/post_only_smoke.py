#!/usr/bin/env python3
from __future__ import annotations

"""
Post-Only smoke on Lighter testnet: submit a post-only LIMIT order in two scenarios
- accept: price does not cross the book -> Accepted, should not fill immediately.
- reject: price crosses the book -> venue should reject (or not accept) the order.

Usage:
  source ~/nautilus_trader-develop/.venv/bin/activate
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python scripts/lighter_tests/post_only_smoke.py \
    --credentials ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --market-index 1 \
    --side buy \
    --scenario accept \
    --outfile /tmp/lighter_postonly.jsonl \
    --runtime 60
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
from nautilus_trader.adapters.lighter.common.enums import LighterTimeInForce, LighterOrderType


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


async def record(ws_url: str, auth: str | None, account: int, outfile: str, runtime: int) -> None:
    async with websockets.connect(ws_url) as ws:
        subs = [
            {"type": "subscribe", "channel": f"account_all/{account}"},
            {"type": "subscribe", "channel": f"account_all_orders/{account}"},
            {"type": "subscribe", "channel": f"account_all_trades/{account}"},
        ]
        if auth:
            for s in subs:
                s["auth"] = auth
        for s in subs:
            await ws.send(json.dumps(s))
        loop = asyncio.get_event_loop()
        end = loop.time() + runtime
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


async def main() -> int:
    import logging
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    logger = logging.getLogger("lighter_test.post_only")
    ap = argparse.ArgumentParser()
    ap.add_argument("--credentials", required=True)
    ap.add_argument("--http", required=True)
    ap.add_argument("--ws", required=True)
    ap.add_argument("--market-index", type=int, required=True)
    ap.add_argument("--side", choices=["buy", "sell"], default="buy")
    ap.add_argument("--scenario", choices=["accept", "reject"], default="accept")
    ap.add_argument("--outfile", default="/tmp/lighter_postonly.jsonl")
    ap.add_argument("--runtime", type=int, default=60)
    ap.add_argument("--base-mult", type=float, default=1.0, help="multiply min_base_amount by this factor")
    ap.add_argument("--cancel-after", type=int, default=None, help="seconds to wait then cancel-all (optional)")
    args = ap.parse_args()

    creds = LighterCredentials.from_json_file(args.credentials)
    signer = LighterSigner(credentials=creds, base_url=args.http, chain_id=300)
    token = signer.create_auth_token(deadline_seconds=600)
    http_pub = LighterPublicHttpClient(args.http)
    http_acc = LighterAccountHttpClient(args.http)
    http_tx = LighterTransactionHttpClient(args.http)

    # Decimals + book
    details = await http_pub.get_order_book_details(args.market_index)
    try:
        price_decimals = int(details.price_decimals or 2)
        size_decimals = int(details.size_decimals or 4)
    except AttributeError:
        price_decimals = int(details.get("price_decimals", 2))
        size_decimals = int(details.get("size_decimals", 4))
    ob = await http_pub.get_order_book_orders(args.market_index, limit=5)
    best_bid, best_ask = _best_px(ob)
    logger.info("[DBG] decimals price=%s size=%s best_bid=%s best_ask=%s", price_decimals, size_decimals, best_bid, best_ask)
    if best_bid is None and best_ask is None:
        raise SystemExit("No top-of-book; pick an active market")
    px_tick = 10 ** (-price_decimals)
    scale_px = 10 ** price_decimals
    scale_sz = 10 ** size_decimals
    # min base amount
    summaries = await http_pub.get_order_books()
    min_base = 0.0
    for s in summaries:
        try:
            if int(s.get("market_id")) == int(args.market_index):
                mb = s.get("min_base_amount")
                min_base = float(mb) if mb is not None else 0.0
                break
        except Exception:
            continue
    mult = max(1.0, float(args.base_mult))
    base_amount_i = max(1, int(math.ceil(((min_base or (1.0 / scale_sz)) * mult) * scale_sz)))

    def _i(px: float) -> int:
        return max(1, int(px * scale_px + 1e-9))

    # Compute target price
    side_buy = args.side == "buy"
    if args.scenario == "accept":
        # post-only should rest: BUY below ask (but ideally >= bid), SELL above bid (<= ask)
        if side_buy:
            target = (best_ask or best_bid) - 2 * px_tick
            if best_bid is not None:
                target = max(target, best_bid + px_tick)  # rest on bid side
        else:
            target = (best_bid or best_ask) + 2 * px_tick
            if best_ask is not None:
                target = min(target, best_ask - px_tick)
    else:  # reject
        # make it cross: BUY at/above ask, SELL at/below bid
        if side_buy:
            target = (best_ask or best_bid) + px_tick
        else:
            target = (best_bid or best_ask) - px_tick

    logger.info("[DBG] target_price=%s px_tick=%s", target, px_tick)
    price_i = _i(target)
    is_ask = not side_buy

    # Local nonce manager: fetch once, increment thereafter
    class _LocalNonce:
        def __init__(self, start: int) -> None:
            self._n = int(start)
        def next(self) -> int:
            v = self._n
            self._n += 1
            return v
    lnonce = _LocalNonce(await http_acc.get_next_nonce(creds.account_index, creds.api_key_index))
    # Submit post-only limit
    coi = int(time.time_ns() & ((1 << 48) - 1))
    logger.info("[DBG] base_amount_i=%s price_i=%s is_ask=%s tif=POST_ONLY", base_amount_i, price_i, is_ask)
    tx, err = await signer.sign_create_order_tx(
        market_index=int(args.market_index),
        client_order_index=int(coi),
        base_amount=base_amount_i,
        price=price_i,
        is_ask=is_ask,
        order_type=LighterOrderType.LIMIT,
        time_in_force=LighterTimeInForce.POST_ONLY,
        reduce_only=False,
        trigger_price=0,
        order_expiry=-1,
        nonce=int(lnonce.next()),
    )
    if err:
        print("sign error:", err)
    else:
        res = await http_tx.send_tx(tx_type=signer.get_tx_type_create_order(), tx_info=tx, auth=token)
        logger.info("[POST_ONLY] sendTx -> code=%s message=%s", res.get("code"), res.get("message"))

    # Optional cancel after a delay to force a cancel callback
    if args.cancel_after is not None and args.cancel_after > 0:
        await asyncio.sleep(float(args.cancel_after))
        lnonce = await http_acc.get_next_nonce(creds.account_index, creds.api_key_index)
        logger.info("[DBG] cancel_all nonce=%s", lnonce)
        from nautilus_trader.adapters.lighter.common.enums import LighterCancelAllTif
        tx_all, err_c = await signer.sign_cancel_all_orders_tx(LighterCancelAllTif.IMMEDIATE, 0, int(lnonce))
        if not err_c and tx_all:
            res_c = await http_tx.send_tx(tx_type=signer.get_tx_type_cancel_all(), tx_info=tx_all, auth=token)
            logger.info("[CANCEL_ALL] -> code=%s message=%s", res_c.get("code"), res_c.get("message"))

    # Record callbacks window
    await record(args.ws, token, creds.account_index, args.outfile, args.runtime)
    print("Recorded:", args.outfile)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
