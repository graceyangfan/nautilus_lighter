from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.lighter_tests.context import LighterTestContext, configure_logging


logger = logging.getLogger("lighter_test.audit")


@dataclass(slots=True)
class PositionDelta:
    market_index: int
    symbol: str | None
    before: float
    after: float


def _scaled_amount(value: float, decimals: int) -> int:
    return int(round(value * (10**decimals)))


async def _flatten_positions(context: LighterTestContext, *, auth: str, price_margin: float) -> list[PositionDelta]:
    overview_before = await context.http_account.get_account_overview(
        account_index=context.credentials.account_index,
        auth=auth,
    )
    positions = overview_before.get("positions") if isinstance(overview_before, dict) else []
    if not positions:
        logger.info("no positions to flatten")
        return []

    deltas: list[PositionDelta] = []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        position_str = pos.get("position")
        symbol = pos.get("symbol")
        market_id = pos.get("market_id")
        sign = pos.get("sign")
        if position_str is None or market_id is None or sign is None:
            continue
        try:
            qty = float(position_str)
        except Exception:
            continue
        if abs(qty) < 1e-10:
            continue
        size_decimals = 0
        price_decimals = 0
        try:
            details = await context.http_public.get_order_book_details(market_id=market_id)
            if isinstance(details, dict):
                size_decimals = int(details.get("size_decimals") or details.get("supported_size_decimals") or 0)
                price_decimals = int(details.get("price_decimals") or details.get("supported_price_decimals") or 0)
        except Exception:
            size_decimals = 0
        base_amount = _scaled_amount(abs(qty), size_decimals)
        if base_amount <= 0:
            continue
        mark = await context.fetch_mark_price(market_id)
        if mark is None:
            logger.warning("missing mark price for %s (market %s)", symbol, market_id)
            mark = 1
        best_bid, best_ask = await context.fetch_best_prices(market_id)
        is_close_long = qty > 0
        if is_close_long:
            anchor_ask = best_ask if best_ask is not None else mark
            raw_price = max(anchor_ask, int(mark * (1 + price_margin)))
            tif = 0
            order_type_flag = 0
            is_ask = True
        else:
            anchor_bid = best_bid if best_bid is not None else mark
            raw_price = min(anchor_bid, int(mark * (1 - price_margin)))
            tif = 0
            order_type_flag = 0
            is_ask = False
        price = await context.clamp_price(
            market_id,
            raw_price,
            is_ask=is_close_long,
        )
        tx_type = context.signer.get_tx_type_create_order()
        nonce = await context.fetch_next_nonce()
        client_index = int.from_bytes(os.urandom(6), "big")
        tx_info, err = await context.signer.sign_create_order_tx(
            market_index=market_id,
            client_order_index=client_index,
            base_amount=base_amount,
            price=price,
            is_ask=is_ask,
            order_type=order_type_flag,
            time_in_force=tif,
            reduce_only=True,
            trigger_price=0,
            order_expiry=-1 if tif != 0 else 0,
            nonce=nonce,
        )
        if err:
            logger.error("flatten %s market %s sign error: %s", symbol, market_id, err)
            continue
        try:
            resp = await context.http_tx.send_tx(
                tx_type=tx_type,
                tx_info=tx_info,
            )
            logger.info("flatten %s market=%s qty=%s tx_hash=%s", symbol, market_id, qty, resp.get("tx_hash"))
        except Exception as exc:
            logger.error("flatten %s market=%s failed: %s", symbol, market_id, exc)
            continue
        deltas.append(PositionDelta(market_index=market_id, symbol=symbol, before=qty, after=0.0))

    return deltas


async def _run_ws_monitor(context: LighterTestContext, auth: str, account_index: int, run_secs: int) -> None:
    async def handler(msg: dict[str, Any]) -> None:
        t = msg.get("type")
        if t in {"connected", "ping"}:
            return
        channel = msg.get("channel")
        logger.info("WS %s %s %s", t, channel, json.dumps(msg.get("data") or msg, ensure_ascii=False))

    subs = [
        {"type": "subscribe", "channel": f"account_all/{account_index}", "auth": auth},
        {"type": "subscribe", "channel": f"account_all_orders/{account_index}", "auth": auth},
        {"type": "subscribe", "channel": f"account_all_trades/{account_index}", "auth": auth},
        {"type": "subscribe", "channel": f"account_all_positions/{account_index}", "auth": auth},
    ]

    await context.run_ws_session(handler=handler, subscriptions=subs, run_secs=run_secs)


async def run_audit(
    *,
    credentials_path: Path,
    base_http: str,
    base_ws: str | None,
    run_ws_secs: int,
    flatten: bool,
    price_margin: float,
) -> None:
    context = LighterTestContext(
        credentials_path=credentials_path,
        base_http=base_http,
        base_ws=base_ws,
    )
    try:
        auth = context.auth_token()
        overview = await context.http_account.get_account_overview(
            account_index=context.credentials.account_index,
            auth=auth,
        )
        logger.info("overview=%s", json.dumps(overview, ensure_ascii=False))
        orders = await context.http_account.get_account_orders(
            account_index=context.credentials.account_index,
            auth=auth,
        )
        logger.info("orders=%s", json.dumps(orders, ensure_ascii=False))
        positions = await context.http_account.get_account_positions(
            account_index=context.credentials.account_index,
            auth=auth,
        )
        logger.info("positions=%s", json.dumps(positions, ensure_ascii=False))
        if run_ws_secs > 0:
            await _run_ws_monitor(context, auth, context.credentials.account_index, run_ws_secs)
        deltas: list[PositionDelta] = []
        if flatten:
            deltas = await _flatten_positions(context, auth=auth, price_margin=price_margin)
        if deltas:
            logger.info("flattened positions: %s", deltas)
            await asyncio.sleep(5)
            post = await context.http_account.get_account_positions(
                account_index=context.credentials.account_index,
                auth=auth,
            )
            logger.info("positions_after=%s", json.dumps(post, ensure_ascii=False))
    finally:
        await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lighter account audit and optional flatten")
    parser.add_argument("--credentials", default=os.getenv("LIGHTER_CREDENTIALS_JSON", "secrets/lighter_testnet_account.json"))
    parser.add_argument("--http", default=os.getenv("LIGHTER_HTTP", "https://testnet.zklighter.elliot.ai"))
    parser.add_argument("--ws", default=os.getenv("LIGHTER_WS"))
    parser.add_argument("--ws-secs", type=int, default=int(os.getenv("LIGHTER_AUDIT_WS_SECS", "0")))
    parser.add_argument("--flatten", action="store_true")
    parser.add_argument("--price-margin", type=float, default=float(os.getenv("LIGHTER_FLATTEN_MARGIN", "0.01")))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=not args.quiet)
    asyncio.run(
        run_audit(
            credentials_path=Path(args.credentials),
            base_http=args.http,
            base_ws=args.ws,
            run_ws_secs=args.ws_secs,
            flatten=args.flatten,
            price_margin=args.price_margin,
        )
    )


if __name__ == "__main__":
    main()
