from __future__ import annotations

"""
Market roundtrip on testnet using signer + REST + WS account streams.

Flow:
- Subscribe private WS (account_all + account_all_orders).
- Compute MARKET IOC open order with target notional (default 10 USDC).
- Sign using lighter-go + fresh /api/v1/nextNonce and send via REST sendTx.
- Observe account_all trades/positions for a window.
- Sign MARKET IOC reduce_only close order and send via REST sendTx.
- Observe account_all updates and print concise summaries.

Usage:
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python -m nautilus_trader.adapters.lighter.dev.quick_market_roundtrip \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --market-id 0 \
    --side long \
    --notional 10 \
    --cancel-delay 60 \
    --runtime 120
"""

import argparse
import asyncio
import logging
from typing import Any

import msgspec

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner
from nautilus_trader.adapters.lighter.websocket.client import LighterWebSocketClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.adapters.lighter.http.transaction import LighterTransactionHttpClient
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
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--notional", type=float, default=10.0, help="target notional in USDC for open")
    ap.add_argument("--cancel-delay", type=int, default=60, help="seconds to wait before close")
    ap.add_argument("--runtime", type=int, default=120, help="total seconds to run")
    args = ap.parse_args()

    logging.getLogger().setLevel(logging.INFO)
    clock = LiveClock()
    Logger("quick_mkt_rt").info("Starting market roundtrip...", LogColor.BLUE)

    creds = LighterCredentials.from_json_file(args.secrets)
    signer = LighterSigner(credentials=creds, base_url=args.http, chain_id=300)
    if not signer.available():
        print("Signer unavailable; ensure adapters/lighter/signers has the platform binary")
        return 2

    http_pub = LighterPublicHttpClient(args.http)
    http_acc = LighterAccountHttpClient(args.http)
    http_tx = LighterTransactionHttpClient(args.http)

    async def fresh_nonce() -> int:
        return int(await http_acc.get_next_nonce(creds.account_index, creds.api_key_index))

    # WS connect & subscribe private
    token = signer.create_auth_token(deadline_seconds=600)
    state: dict[str, Any] = {
        "open_coi": None,
        "open_oi": None,
        "close_coi": None,
        "close_oi": None,
        "trades": 0,
        "positions_seen": 0,
        "orders_seen": 0,
    }
    def handler(raw: bytes) -> None:
        try:
            obj = msgspec.json.decode(raw)
        except msgspec.DecodeError:
            return
        t = obj.get("type") if isinstance(obj, dict) else None
        ch = obj.get("channel") if isinstance(obj, dict) else None
        if isinstance(ch, str) and ch.startswith("account_all"):
            data = obj.get("data") or {}
            orders = data.get("orders") or {}
            trades = data.get("trades") or {}
            positions = data.get("positions") or {}
            # orders
            vals = list(orders.values()) if isinstance(orders, dict) else list(orders)
            state["orders_seen"] += len(vals)
            for od in vals:
                oi = od.get("order_index")
                coi = od.get("client_order_index")
                st = od.get("status")
                if state["open_oi"] is None:
                    state["open_oi"] = oi
            tv = list(trades.values()) if isinstance(trades, dict) else list(trades)
            state["trades"] += len(tv)
            pv = list(positions.values()) if isinstance(positions, dict) else list(positions)
            state["positions_seen"] += len(pv)

    ws = LighterWebSocketClient(
        clock=clock,
        base_url=args.ws,
        handler=handler,
        handler_reconnect=None,
        loop=asyncio.get_event_loop(),
        ws_send_quota_per_sec=3,
    )
    await ws.connect()
    await ws.wait_until_ready(10)
    acct = creds.account_index
    await ws.send({"type": "subscribe", "channel": f"account_all/{acct}", "auth": token})
    await ws.send({"type": "subscribe", "channel": f"account_all_orders/{acct}", "auth": token})

    # Infer decimals and min_base_amount
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
    scale_px = 10 ** price_decimals
    scale_sz = 10 ** size_decimals

    # Compute price references for MARKET (pass best side as reference price)
    best_bid, best_ask = await _best_prices(http_pub, int(args.market_id))
    if best_bid is None and best_ask is None:
        best_bid, best_ask = 2000.0, 2010.0
    is_open_ask = (args.side.lower() == "short")  # short -> sell; long -> buy
    ref_px = best_ask if not is_open_ask else best_bid  # buy uses ask; sell uses bid
    ref_px_i = max(1, int(ref_px * scale_px))

    # Compute size for target notional (>= min_base_amount)
    notional = float(args.notional)
    qty_f = max(min_base_amount, notional / max(ref_px, 1e-9))
    step = 10 ** (-size_decimals)
    import math
    qty_f = math.ceil(qty_f / step) * step
    qty_i = max(1, int(qty_f * scale_sz + 1e-9))

    # Open: MARKET IOC create_order
    from nautilus_trader.adapters.lighter.common.enums import LighterOrderType, LighterTimeInForce
    open_coi = int(clock.timestamp_ns() & ((1 << 48) - 1))
    state["open_coi"] = open_coi
    n_open = await fresh_nonce()
    tx_open, err = await signer.sign_create_order_tx(
        market_index=int(args.market_id),
        client_order_index=open_coi,
        base_amount=qty_i,
        price=ref_px_i,
        is_ask=is_open_ask,
        order_type=LighterOrderType.MARKET,
        time_in_force=LighterTimeInForce.IOC,
        reduce_only=False,
        trigger_price=0,
        order_expiry=0,
        nonce=n_open,
    )
    if err or not tx_open:
        print("[open] sign failed:", err)
        return 2
    res_open = await http_tx.send_tx(tx_type=signer.get_tx_type_create_order(), tx_info=tx_open, auth=token)
    print("[open] sendTx code=", res_open.get("code"), "tx_hash=", res_open.get("tx_hash"), "qty_i=", qty_i, "px_i=", ref_px_i)

    # Observe before close
    end_open = clock.timestamp_ns() + max(1, int(args.cancel_delay)) * 1_000_000_000
    while clock.timestamp_ns() < end_open:
        await asyncio.sleep(1.0)

    # Close: MARKET IOC reduce_only opposite side
    close_is_ask = not is_open_ask
    close_coi = int((clock.timestamp_ns() ^ 0xABCDEF) & ((1 << 48) - 1))
    state["close_coi"] = close_coi
    # Recompute ref price for close
    best_bid2, best_ask2 = await _best_prices(http_pub, int(args.market_id))
    if best_bid2 is None and best_ask2 is None:
        best_bid2, best_ask2 = best_bid, best_ask
    ref_px2 = best_ask2 if not close_is_ask else best_bid2
    ref_px2_i = max(1, int(ref_px2 * scale_px))
    n_close = await fresh_nonce()
    tx_close, errc = await signer.sign_create_order_tx(
        market_index=int(args.market_id),
        client_order_index=close_coi,
        base_amount=qty_i,
        price=ref_px2_i,
        is_ask=close_is_ask,
        order_type=LighterOrderType.MARKET,
        time_in_force=LighterTimeInForce.IOC,
        reduce_only=True,
        trigger_price=0,
        order_expiry=0,
        nonce=n_close,
    )
    if errc or not tx_close:
        print("[close] sign failed:", errc)
        return 3
    res_close = await http_tx.send_tx(tx_type=signer.get_tx_type_create_order(), tx_info=tx_close, auth=token)
    print("[close] sendTx code=", res_close.get("code"), "tx_hash=", res_close.get("tx_hash"), "qty_i=", qty_i, "px_i=", ref_px2_i)

    # Final observation window
    end_all = clock.timestamp_ns() + max(1, int(args.runtime)) * 1_000_000_000
    while clock.timestamp_ns() < end_all:
        await asyncio.sleep(1.0)

    await ws.disconnect()
    print("[summary] orders_seen=", state["orders_seen"], "trades=", state["trades"], "positions_seen=", state["positions_seen"])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
