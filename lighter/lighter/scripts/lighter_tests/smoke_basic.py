from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path

from scripts.lighter_tests.context import LighterTestContext, configure_logging
try:
    from nautilus_trader.adapters.lighter.common.enums import (
        LighterOrderType,
        LighterTimeInForce,
    )
except Exception:
    # Fallback constants for older installed versions
    class _OT:
        LIMIT = 0
    class _TIF:
        GTT = 1
    LighterOrderType = _OT  # type: ignore[assignment]
    LighterTimeInForce = _TIF  # type: ignore[assignment]


async def run_smoke(context: LighterTestContext, market_index: int, base_amount: int, price_span: float, ws_runtime: int) -> None:
    logger = logging.getLogger("lighter_test.smoke")

    token = context.auth_token()
    account = await context.http_account.get_account_overview(account_index=context.credentials.account_index, auth=token)
    logger.info("Account overview fetched: keys=%s", list(account.keys()))

    next_nonce = await context.fetch_next_nonce()
    logger.info("Next nonce: %s", next_nonce)

    mark_price = await context.fetch_mark_price(market_index)
    bid, ask = await context.fetch_best_prices(market_index)
    if mark_price is None and bid is None and ask is None:
        logger.warning("No market price detected; using fallback 200000")
        mark_price = 200000
        bid = 200000
        ask = 201000

    if bid is not None:
        base_price = bid
    elif mark_price is not None:
        base_price = mark_price
    elif ask is not None:
        base_price = ask
    else:
        base_price = 200000

    def _scaled(px: int | None, multiplier: float) -> int:
        if px is None:
            return 200000
        return max(1, int(px * multiplier))

    offset = max(price_span / 2, 0.001)
    price_lo = _scaled(base_price, 1.0 - offset)
    price_hi = _scaled(base_price, 1.0 - offset / 2)
    logger.info(
        "price basis mark=%s bid=%s ask=%s -> entry=%s tp=%s",
        mark_price,
        bid,
        ask,
        price_lo,
        price_hi,
    )

    num_orders = 2
    tx_type_list = [context.signer.get_tx_type_create_order()] * num_orders
    rest_response = None
    entry_used = None
    exit_used = None
    price_candidates = [
        (price_lo, price_hi),
        (_scaled(base_price, 1.0 - offset / 2), max(1, _scaled(base_price, 1.0 - offset / 2) - 10)),
        (_scaled(base_price, 1.0 - offset / 4), max(1, _scaled(base_price, 1.0 - offset / 4) - 10)),
    ]
    for entry_price, exit_price in price_candidates:
        try:
            payloads = []
            base_nonce = await context.fetch_next_nonce()
            prices_used: list[int] = []
            for idx in range(num_orders):
                raw_price = entry_price if idx == 0 else exit_price
                clamped_price = await context.clamp_price(
                    market_index,
                    raw_price,
                    is_ask=False,
                )
                prices_used.append(clamped_price)
                tx, err = await context.signer.sign_create_order_tx(
                    market_index=market_index,
                    client_order_index=random.randint(1 << 20, 1 << 28),
                    base_amount=base_amount,
                    price=clamped_price,
                    is_ask=False,
                    order_type=LighterOrderType.LIMIT,
                    time_in_force=LighterTimeInForce.GTT,
                    reduce_only=False,
                    trigger_price=0,
                    order_expiry=-1,
                    nonce=base_nonce + idx,
                )
                if err:
                    raise RuntimeError(err)
                payloads.append(tx)
            rest_response = await context.http_tx.send_tx_batch(
                tx_types=tx_type_list,
                tx_infos=payloads,
                auth=token,
            )
            entry_used, exit_used = prices_used
            batch_payload = payloads
            logger.info("REST sendTxBatch response: %s", rest_response)
            break
        except Exception as exc:
            logger.warning("send_tx_batch retry entry=%s exit=%s error=%s", entry_price, exit_price, exc)
            await asyncio.sleep(1)
            continue
    if rest_response is None:
        raise RuntimeError("sendTxBatch failed for all price candidates")

    nonce_ws = await context.fetch_next_nonce()
    ws_payloads: list[str] = []
    ws_types = [context.signer.get_tx_type_create_order()] * num_orders
    entry_ws = entry_used or price_lo
    exit_ws = exit_used or price_hi
    for idx in range(num_orders):
        desired_price = entry_ws if idx == 0 else exit_ws
        clamped_ws = await context.clamp_price(
            market_index,
            desired_price,
            is_ask=False,
        )
        tx_ws, err_ws = await context.signer.sign_create_order_tx(
            market_index=market_index,
            client_order_index=random.randint(1 << 20, 1 << 28),
            base_amount=base_amount,
            price=clamped_ws,
            is_ask=False,
            order_type=LighterOrderType.LIMIT,
            time_in_force=LighterTimeInForce.GTT,
            reduce_only=False,
            trigger_price=0,
            order_expiry=-1,
            nonce=nonce_ws + idx,
        )
        if err_ws:
            raise RuntimeError(f"ws sign_create_order error: {err_ws}")
        ws_payloads.append(tx_ws)

    async def handler(msg: dict) -> None:
        if msg.get("type") == "connected":
            logger.info("WS connected session_id=%s", msg.get("session_id"))
            return
        if msg.get("type") in {"subscribed/account_all", "subscribed/account_all_orders", "subscribed/account_all_positions"}:
            logger.info("WS subscribed: %s", msg.get("channel"))
            return
        if msg.get("type") in {"jsonapi/sendtx", "jsonapi/sendtxbatch"}:
            logger.info("WS tx response: %s", msg)
        elif msg.get("channel", "").startswith("account_all_orders:"):
            logger.info("WS orders snapshot entries=%s", len((msg.get("data") or {}).get("orders", [])))
        elif msg.get("type") == "ping":
            logger.debug("WS ping received")

    subs = [
        {"type": "subscribe", "channel": f"account_all/{context.credentials.account_index}", "auth": token},
        {"type": "subscribe", "channel": f"account_all_orders/{context.credentials.account_index}", "auth": token},
        {"type": "subscribe", "channel": f"account_all_positions/{context.credentials.account_index}", "auth": token},
    ]

    async def ws_runner():
        await context.run_ws_session(
            handler=handler,
            subscriptions=subs
            + [
                {
                    "type": "jsonapi/sendtxbatch",
                    "data": {
                        "id": f"smoke-{time.time_ns()}",
                        "tx_types": json.dumps(ws_types),
                        "tx_infos": json.dumps(ws_payloads),
                    },
                }
            ],
            run_secs=ws_runtime,
        )

    try:
        await ws_runner()
    finally:
        await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lighter adapter smoke test")
    parser.add_argument("--credentials", default=os.getenv("LIGHTER_CREDENTIALS_JSON", "secrets/lighter_testnet_account.json"))
    parser.add_argument("--http", default=os.getenv("LIGHTER_HTTP", "https://testnet.zklighter.elliot.ai"))
    parser.add_argument("--ws", default=os.getenv("LIGHTER_WS"))
    parser.add_argument("--market-index", type=int, default=int(os.getenv("LIGHTER_MARKET_INDEX", "1")))
    parser.add_argument("--base-amount", type=int, default=int(os.getenv("LIGHTER_BASE_AMOUNT", "20")))
    parser.add_argument("--price-span", type=float, default=float(os.getenv("LIGHTER_PRICE_SPAN", "0.01")))
    parser.add_argument("--ws-runtime", type=int, default=int(os.getenv("LIGHTER_RUN_SECS", "60")))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=not args.quiet)

    context = LighterTestContext(
        credentials_path=Path(args.credentials),
        base_http=args.http,
        base_ws=args.ws,
    )
    asyncio.run(
        run_smoke(
            context=context,
            market_index=args.market_index,
            base_amount=args.base_amount,
            price_span=args.price_span,
            ws_runtime=args.ws_runtime,
        )
    )


if __name__ == "__main__":
    main()
