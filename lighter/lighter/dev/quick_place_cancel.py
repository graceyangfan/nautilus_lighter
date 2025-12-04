from __future__ import annotations

"""
Minimal execution smoke on testnet using signer + WS client only.

Flow:
 1) Load credentials and create lighter-go signer (ctypes backend)
 2) Fetch next nonce via REST once, locally increment thereafter
 3) Connect WS and subscribe private channels with auth token
 4) Place a far-away LIMIT order (to avoid fills) via jsonapi/sendtx
 5) Wait, then send a cancel-all via jsonapi/sendtx
 6) Print concise sendtx_result and account_all snapshots

Usage:
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python -m nautilus_trader.adapters.lighter.dev.quick_place_cancel \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --market-id 0 \
    --side buy \
    --ticks 1 \
    --qty 0.005 \
    --runtime 30
"""

import argparse
import asyncio
import logging
from typing import Any

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner
from nautilus_trader.adapters.lighter.websocket.client import LighterWebSocketClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.common.component import LiveClock, Logger
from nautilus_trader.common.enums import LogColor


async def _best_prices(http_pub: LighterPublicHttpClient, mid: int) -> tuple[float | None, float | None]:
    try:
        doc = await http_pub.get_order_book_orders(mid, limit=5)
        def first_px(arr):
            if not isinstance(arr, list) or not arr:
                return None
            x = arr[0].get("price")
            return float(x) if x is not None else None
        return first_px(doc.get("bids")), first_px(doc.get("asks"))
    except Exception as exc:
        logging.getLogger(__name__).warning("best_prices fetch failed: %s", exc)
        return None, None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secrets", required=True)
    ap.add_argument("--http", required=True)
    ap.add_argument("--ws", required=True)
    ap.add_argument("--market-id", type=int, default=0)
    ap.add_argument("--side", choices=["buy", "sell"], default="buy")
    ap.add_argument("--ticks", type=int, default=1, help="price offset ticks from top of book")
    ap.add_argument("--qty", type=float, default=None, help="base quantity (unscaled); defaults to market min")
    ap.add_argument("--runtime", type=int, default=30)
    ap.add_argument("--notional-usdc", type=float, default=10.0, help="target notional in USDC (e.g. 10)")
    args = ap.parse_args()

    logging.getLogger().setLevel(logging.INFO)
    clock = LiveClock()
    Logger("quick_place_cancel").info("Starting execution smoke...", LogColor.BLUE)

    creds = LighterCredentials.from_json_file(args.secrets)
    signer = LighterSigner(credentials=creds, base_url=args.http, chain_id=300)
    if not signer.available():
        print("Signer unavailable; ensure adapters/lighter/signers has the platform binary")
        return 2

    http_pub = LighterPublicHttpClient(args.http)
    http_acc = LighterAccountHttpClient(args.http)

    # Helper: always fetch fresh nonce before signing
    async def _fresh_nonce() -> int:
        return int(await http_acc.get_next_nonce(creds.account_index, creds.api_key_index))
    cur_nonce = await _fresh_nonce()
    print("Fresh nonce:", cur_nonce)

    # WS connect
    token = signer.create_auth_token(deadline_seconds=600)
    ws = LighterWebSocketClient(
        clock=clock,
        base_url=args.ws,
        handler=lambda raw: None,
        handler_reconnect=None,
        loop=asyncio.get_event_loop(),
        ws_send_quota_per_sec=3,
    )
    await ws.connect()
    await ws.wait_until_ready(10)

    acct = creds.account_index
    # Subscribe private channels
    await ws.send({"type": "subscribe", "channel": f"account_all/{acct}", "auth": token})
    await ws.send({"type": "subscribe", "channel": f"account_all_orders/{acct}", "auth": token})

    # Wrap ws handler to print concise updates
    # Track latest order_index seen for targeted cancel
    state: dict[str, Any] = {"last_order_index": None, "last_market_id": None, "seen_for_coi": False, "our_status": None}

    def handler(raw: bytes) -> None:
        try:
            import msgspec
            obj = msgspec.json.decode(raw)
        except Exception:
            return
        t = obj.get("type") if isinstance(obj, dict) else None
        ch = obj.get("channel") if isinstance(obj, dict) else None
        if t in ("jsonapi/sendtx_result", "jsonapi/sendtxbatch_result"):
            data = obj.get("data") or {}
            print("[sendtx]", t, {k: data.get(k) for k in ("status", "code", "tx_hash", "error", "message")})
        if isinstance(ch, str) and ch.startswith("account_all"):
            data = obj.get("data") or {}
            orders = data.get("orders") or {}
            # unified view of orders list
            if isinstance(orders, dict):
                vals = list(orders.values())
            else:
                vals = list(orders)
            for od in vals:
                mid = od.get("market_id") if isinstance(od, dict) else None
                oi = od.get("order_index") if isinstance(od, dict) else None
                coi = od.get("client_order_index") if isinstance(od, dict) else None
                st = od.get("status") if isinstance(od, dict) else None
                if oi is not None:
                    state["last_order_index"] = oi
                    state["last_market_id"] = mid
                # mark if it's our order (by client_order_index)
                if state.get("target_coi") is not None and coi is not None and int(str(coi)) == int(state["target_coi"]):
                    state["seen_for_coi"] = True
                    state["our_status"] = st
            print("[account_all]", ch.split(":")[0], "orders_seen=", len(vals))

    ws._handler = handler  # type: ignore[attr-defined]

    # Prepare near-top LIMIT order (price close to top_of_book with given ticks)
    # Load market details (decimals) and summary (min_base_amount)
    details = await http_pub.get_order_book_details(int(args.market_id))
    summaries = await http_pub.get_order_books()
    price_decimals = int((details or {}).get("price_decimals", 2))
    size_decimals = int((details or {}).get("size_decimals", 4))
    min_base_amount = 0.0
    for s in summaries:
        try:
            if int(s.get("market_id")) == int(args.market_id):
                min_base_amount = float(s.get("min_base_amount") or 0.0)
                break
        except (TypeError, ValueError):
            continue
    if min_base_amount <= 0:
        min_base_amount = 0.001
    # Fetch best prices
    best_bid, best_ask = await _best_prices(http_pub, int(args.market_id))
    if best_bid is None and best_ask is None:
        best_bid, best_ask = 2000.0, 2010.0
    tick = 10 ** (-price_decimals)
    # Compute target price close to book (non-marketable)
    side = args.side.lower()
    ticks = max(0, int(args.ticks))
    if side == "buy":
        # bid + ticks, but strictly below ask
        target = (best_bid or 0.0) + ticks * tick
        if best_ask is not None:
            target = min(target, best_ask - tick)
        is_ask = False
    else:
        # ask - ticks, but strictly above bid
        target = (best_ask or 0.0) - ticks * tick
        if best_bid is not None:
            target = max(target, best_bid + tick)
        is_ask = True
    # Scale price and size to integers required by signer
    scale_px = 10 ** price_decimals
    scale_sz = 10 ** size_decimals
    # Round price to tick (integer space)
    bid_i = int((best_bid or 0.0) * scale_px)
    ask_i = int((best_ask or 0.0) * scale_px)
    tgt_i = int(target * scale_px)
    if side == "buy" and best_ask is not None:
        tgt_i = min(tgt_i, ask_i - 1)
    if side == "sell" and best_bid is not None:
        tgt_i = max(tgt_i, bid_i + 1)
    # Quantity
    # target notional: qty ~= notional_usdc / target_price
    notional = float(args.notional_usdc)
    qty_f = notional / max(target, 1e-9)
    # enforce min_base_amount
    qty_f = max(qty_f, float(min_base_amount))
    # if user passed qty, override to max(user_qty, min_base_amount)
    if args.qty is not None:
        qty_f = max(qty_f, float(args.qty))
    # quantize to step (size_decimals)
    step = 10 ** (-size_decimals)
    import math
    qty_f = math.ceil(qty_f / step) * step
    qty_i = max(1, int(qty_f * scale_sz + 1e-9))

    # Place order (create_order)
    from nautilus_trader.adapters.lighter.common.enums import LighterOrderType, LighterTimeInForce
    cur_nonce = await _fresh_nonce()
    # unique 48-bit COI
    coi = (clock.timestamp_ns() & ((1 << 48) - 1))
    state["target_coi"] = int(coi)
    tx_info, err = await signer.sign_create_order_tx(
        market_index=int(args.market_id),
        client_order_index=int(coi),
        base_amount=qty_i,
        price=tgt_i,
        is_ask=is_ask,
        order_type=LighterOrderType.LIMIT,
        time_in_force=LighterTimeInForce.GTT,
        reduce_only=False,
        trigger_price=0,
        order_expiry=-1,
        nonce=cur_nonce,
    )
    if err or not tx_info:
        print("sign create_order failed:", err)
    else:
        # Prefer REST sendTx for deterministic acceptance
        from nautilus_trader.adapters.lighter.http.transaction import LighterTransactionHttpClient
        tx_http = LighterTransactionHttpClient(args.http)
        res = await tx_http.send_tx(tx_type=signer.get_tx_type_create_order(), tx_info=tx_info, auth=token)
        print("[REST] sendTx(create) code=", res.get("code"), "tx_hash=", res.get("tx_hash"))
        print("create_order sent (px_i=", tgt_i, ", qty_i=", qty_i, ", nonce=", cur_nonce, ", coi=", int(coi), ")")

    # Wait up to half runtime for our order to appear in account_all
    deadline = clock.timestamp_ns() + max(5, args.runtime // 2) * 1_000_000_000
    while clock.timestamp_ns() < deadline:
        await asyncio.sleep(1.0)
        if state.get("seen_for_coi"):
            print("[observe] our order appeared: status=", state.get("our_status"), "order_index=", state.get("last_order_index"))
            break

    # Try targeted cancel if we have seen an order_index, else fallback to cancel-all
    from nautilus_trader.adapters.lighter.common.enums import LighterCancelAllTif
    cur_nonce = await _fresh_nonce()
    oi = state.get("last_order_index")
    if state.get("seen_for_coi") and state.get("last_order_index") is not None:
        oi = int(state.get("last_order_index"))
        try:
            tx_c, errc = await signer.sign_cancel_order_tx(market_index=int(args.market_id), order_index=oi, nonce=cur_nonce)
            if errc or not tx_c:
                raise RuntimeError(str(errc))
            tx_http = LighterTransactionHttpClient(args.http)
            res = await tx_http.send_tx(tx_type=signer.get_tx_type_cancel_order(), tx_info=tx_c, auth=token)
            print("[REST] sendTx(cancel) code=", res.get("code"), "tx_hash=", res.get("tx_hash"))
            print("cancel_order sent (oi=", oi, ", nonce=", cur_nonce, ")")
        except Exception as e:
            print("cancel_order sign/send failed, fallback cancel_all:", e)
            tx_all, erra = await signer.sign_cancel_all_orders_tx(time_in_force=LighterCancelAllTif.IMMEDIATE, time=0, nonce=cur_nonce)
            if tx_all:
                await ws.send({"type": "jsonapi/sendtx", "data": {"id": f"cancelall-{clock.timestamp_ns()}", "tx_type": signer.get_tx_type_cancel_all(), "tx_info": __import__("json").loads(tx_all), "auth": token}})
                print("cancel_all sent (nonce=", cur_nonce, ")")
    else:
        tx_all, erra = await signer.sign_cancel_all_orders_tx(time_in_force=LighterCancelAllTif.IMMEDIATE, time=0, nonce=cur_nonce)
        if tx_all:
            await ws.send({"type": "jsonapi/sendtx", "data": {"id": f"cancelall-{clock.timestamp_ns()}", "tx_type": signer.get_tx_type_cancel_all(), "tx_info": __import__("json").loads(tx_all), "auth": token}})
            print("cancel_all sent (nonce=", cur_nonce, ")")

    await asyncio.sleep(max(5, args.runtime // 2))
    await ws.disconnect()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
