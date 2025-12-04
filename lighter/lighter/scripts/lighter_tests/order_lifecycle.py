from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

from scripts.lighter_tests.context import LighterTestContext, configure_logging


logger = logging.getLogger("lighter_test.lifecycle")


@dataclass
class LifecycleResult:
    tx_hashes: list[str]
    order_indexes: list[int]
    fills: list[dict[str, str]]


async def place_limit_order(
    context: LighterTestContext,
    *,
    market_index: int,
    base_amount: int,
    price_candidates: list[int],
    is_ask: bool,
    reduce_only: bool,
) -> tuple[str, str, int]:
    last_error: Exception | None = None
    for price in price_candidates:
        try:
            nonce = await context.fetch_next_nonce()
            coid = random.randint(1 << 20, 1 << 28)
            tx, err = await context.signer.sign_create_order_tx(
                market_index=market_index,
                client_order_index=coid,
                base_amount=base_amount,
                price=price,
                is_ask=is_ask,
                order_type=0,
                time_in_force=1,
                reduce_only=reduce_only,
                trigger_price=0,
                order_expiry=-1,
                nonce=nonce,
            )
            if err:
                raise RuntimeError(err)
            resp = await context.http_tx.send_tx(
                tx_type=context.signer.get_tx_type_create_order(),
                tx_info=tx,
            )
            tx_hash = resp.get("tx_hash")
            side = "SELL" if is_ask else "BUY"
            logger.info("place_limit_order %s tx_hash=%s price=%s", side, tx_hash, price)
            return str(tx_hash), str(coid), price
        except Exception as exc:
            logger.warning("place_limit_order failed side=%s price=%s error=%s", "SELL" if is_ask else "BUY", price, exc)
            last_error = exc
            await asyncio.sleep(1)
            continue
    raise RuntimeError(f"Failed to place limit order after attempts: {last_error}")


async def cancel_all(context: LighterTestContext, nonce: int) -> str | None:
    from nautilus_trader.adapters.lighter.common.enums import LighterCancelAllTif
    tx_info, err = await context.signer.sign_cancel_all_orders_tx(time_in_force=LighterCancelAllTif.IMMEDIATE, time=0, nonce=nonce)
    if err:
        logger.warning("cancel_all sign error: %s", err)
        return None
    resp = await context.http_tx.send_tx(
        tx_type=context.signer.get_tx_type_cancel_all(),
        tx_info=tx_info,
    )
    return str(resp.get("tx_hash"))


async def lifecycle_sequence(
    context: LighterTestContext,
    *,
    market_index: int,
    base_amount: int,
    price_span: float,
    wait_secs: int,
) -> LifecycleResult:
    mark_price = await context.fetch_mark_price(market_index)
    bid, ask = await context.fetch_best_prices(market_index)
    logger.info("lifecycle mark=%s bid=%s ask=%s", mark_price, bid, ask)
    if bid is not None:
        base_price = bid
    elif mark_price is not None:
        base_price = mark_price
    elif ask is not None:
        base_price = ask
    else:
        base_price = 200000
    price_entry_candidates = [
        int(base_price * (1 - price_span)),
        int(base_price * (1 - price_span / 2)),
        int(base_price * (1 - price_span / 4)),
    ]
    price_entry_candidates = [max(1, p) for p in price_entry_candidates]
    entry_hash, client_order, price_used = await place_limit_order(
        context,
        market_index=market_index,
        base_amount=base_amount,
        price_candidates=price_entry_candidates,
        is_ask=False,
        reduce_only=False,
    )

    await asyncio.sleep(wait_secs)

    # Reduce-only take-profit
    tp_base = mark_price or base_price
    tp_candidates = [
        int(tp_base * (1 + price_span)),
        int(tp_base * (1 + price_span / 2)),
        int(tp_base * (1 + price_span / 4)),
    ]
    tp_candidates = [max(1, p) for p in tp_candidates]
    tp_hash, _, tp_price_used = await place_limit_order(
        context,
        market_index=market_index,
        base_amount=base_amount,
        price_candidates=tp_candidates,
        is_ask=True,
        reduce_only=True,
    )
    logger.info("take-profit order submitted tx_hash=%s price=%s", tp_hash, tp_price_used)

    await asyncio.sleep(wait_secs)

    nonce_cancel = await context.fetch_next_nonce()
    cancel_hash = await cancel_all(context, nonce_cancel)
    if cancel_hash:
        logger.info("cancel_all hash=%s", cancel_hash)

    overview = await context.http_account.get_account_overview(context.credentials.account_index, auth=context.auth_token())
    logger.info("overview positions=%s", json.dumps(overview.get("positions", [])))

    return LifecycleResult(
        tx_hashes=[entry_hash, tp_hash, cancel_hash or ""],
        order_indexes=[],
        fills=[],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Lighter order lifecycle regression")
    parser.add_argument("--credentials", default=os.getenv("LIGHTER_CREDENTIALS_JSON", "secrets/lighter_testnet_account.json"))
    parser.add_argument("--http", default=os.getenv("LIGHTER_HTTP", "https://testnet.zklighter.elliot.ai"))
    parser.add_argument("--ws", default=os.getenv("LIGHTER_WS"))
    parser.add_argument("--market-index", type=int, default=int(os.getenv("LIGHTER_MARKET_INDEX", "1")))
    parser.add_argument("--base-amount", type=int, default=int(os.getenv("LIGHTER_BASE_AMOUNT", "20")))
    parser.add_argument("--price-span", type=float, default=float(os.getenv("LIGHTER_PRICE_SPAN", "0.01")))
    parser.add_argument("--sleep-secs", type=int, default=int(os.getenv("LIGHTER_SLEEP_SECS", "10")))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=not args.quiet)
    context = LighterTestContext(
        credentials_path=Path(args.credentials),
        base_http=args.http,
        base_ws=args.ws,
    )

    async def runner() -> LifecycleResult:
        res = await lifecycle_sequence(
            context,
            market_index=args.market_index,
            base_amount=args.base_amount,
            price_span=args.price_span,
            wait_secs=args.sleep_secs,
        )
        await context.close()
        return res

    result = asyncio.run(runner())
    logger.info("Lifecycle tx_hashes=%s", result.tx_hashes)


if __name__ == "__main__":
    main()
