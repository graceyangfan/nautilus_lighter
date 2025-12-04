from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from scripts.lighter_tests.context import LighterTestContext, configure_logging


logger = logging.getLogger("lighter_test.faults")


async def price_out_of_range(context: LighterTestContext, market_index: int, base_amount: int) -> None:
    mark_price = await context.fetch_mark_price(market_index) or 200000
    bad_price = int(mark_price * 0.2)
    try:
        nonce = await context.fetch_next_nonce()
        tx_info, err = await context.signer.sign_create_order_tx(
            market_index=market_index,
            client_order_index=99999999,
            base_amount=base_amount,
            price=bad_price,
            is_ask=False,
            order_type=0,
            time_in_force=1,
            reduce_only=False,
            trigger_price=0,
            order_expiry=-1,
            nonce=nonce,
        )
        if err:
            logger.info("expected sign error: %s", err)
            return
        await context.http_tx.send_tx(tx_type=context.signer.get_tx_type_create_order(), tx_info=tx_info)
    except Exception as exc:
        logger.info("price_out_of_range response: %s", exc)


async def nonce_conflict(context: LighterTestContext, market_index: int, base_amount: int) -> None:
    mark_price = await context.fetch_mark_price(market_index) or 200000
    good_price = int(mark_price * 0.995)
    nonce = await context.fetch_next_nonce()
    tx_info, err = await context.signer.sign_create_order_tx(
        market_index=market_index,
        client_order_index=123123,
        base_amount=base_amount,
        price=good_price,
        is_ask=False,
        order_type=0,
        time_in_force=1,
        reduce_only=False,
        trigger_price=0,
        order_expiry=-1,
        nonce=nonce,
    )
    if err:
        logger.warning("sign_create_order failed: %s", err)
        return
    await context.http_tx.send_tx(tx_type=context.signer.get_tx_type_create_order(), tx_info=tx_info)
    try:
        await context.http_tx.send_tx(tx_type=context.signer.get_tx_type_create_order(), tx_info=tx_info)
    except Exception as exc:
        logger.info("nonce conflict response: %s", exc)


async def run_faults(context: LighterTestContext, market_index: int, base_amount: int) -> None:
    await price_out_of_range(context, market_index, base_amount)
    await nonce_conflict(context, market_index, base_amount)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lighter fault injection checks")
    parser.add_argument("--credentials", default=os.getenv("LIGHTER_CREDENTIALS_JSON", "secrets/lighter_testnet_account.json"))
    parser.add_argument("--http", default=os.getenv("LIGHTER_HTTP", "https://testnet.zklighter.elliot.ai"))
    parser.add_argument("--market-index", type=int, default=int(os.getenv("LIGHTER_MARKET_INDEX", "1")))
    parser.add_argument("--base-amount", type=int, default=int(os.getenv("LIGHTER_BASE_AMOUNT", "20")))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=not args.quiet)
    context = LighterTestContext(
        credentials_path=Path(args.credentials),
        base_http=args.http,
    )

    async def runner():
        await run_faults(context, args.market_index, args.base_amount)
        await context.close()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
