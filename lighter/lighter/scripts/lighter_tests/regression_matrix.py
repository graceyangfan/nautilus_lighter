from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.lighter_tests.context import LighterTestContext, configure_logging


logger = logging.getLogger("lighter_test.regression")


@dataclass(slots=True)
class OrderScenario:
    name: str
    order_type: int
    time_in_force: int
    is_ask: bool
    reduce_only: bool
    price_offset: float
    trigger_offset: float | None = None


@dataclass(slots=True)
class ScenarioResult:
    scenario: OrderScenario
    nonce: int
    client_order_index: int
    price_used: int
    trigger_used: int
    tx_hash: str | None
    error: str | None


async def _baseline_prices(context: LighterTestContext, market_index: int) -> dict[str, int]:
    mark = await context.fetch_mark_price(market_index)
    bid, ask = await context.fetch_best_prices(market_index)
    logger.info("baseline mark=%s bid=%s ask=%s", mark, bid, ask)
    fallback = 200_000
    return {
        "mark": mark or fallback,
        "bid": bid or mark or fallback,
        "ask": ask or mark or fallback,
    }


def _scale_price(base: int, offset: float, *, is_ask: bool) -> int:
    factor = 1.0 + offset if is_ask else 1.0 - offset
    return max(1, int(base * factor))


async def _submit_order(
    context: LighterTestContext,
    *,
    market_index: int,
    base_prices: dict[str, int],
    scenario: OrderScenario,
    base_amount: int,
) -> ScenarioResult:
    if scenario.order_type == 1:
        anchor = base_prices["mark"]
        raw_price = _scale_price(anchor, scenario.price_offset or 0.0, is_ask=scenario.is_ask)
        target_for_clamp = anchor
    else:
        if scenario.is_ask:
            anchor = max(base_prices["mark"], base_prices["ask"])
            raw_price = _scale_price(anchor, scenario.price_offset, is_ask=True)
            target_for_clamp = anchor
        else:
            anchor = min(base_prices["mark"], base_prices["bid"])
            raw_price = _scale_price(anchor, scenario.price_offset, is_ask=False)
            target_for_clamp = anchor
    trigger_raw = None
    if scenario.trigger_offset is not None:
        trigger_base = base_prices["mark"]
        trigger_raw = _scale_price(trigger_base, scenario.trigger_offset, is_ask=scenario.is_ask)

    try:
        price = await context.clamp_price(
            market_index,
            raw_price if raw_price else target_for_clamp,
            is_ask=scenario.is_ask,
            trigger_price=trigger_raw,
        )
    except Exception as exc:
        return ScenarioResult(
            scenario=scenario,
            nonce=-1,
            client_order_index=-1,
            price_used=raw_price,
            trigger_used=trigger_raw or 0,
            tx_hash=None,
            error=f"price clamp failed: {exc}",
        )
    if scenario.order_type == 1 and (price is None or price <= 0):
        price = max(raw_price or target_for_clamp or 1, 1)

    nonce = await context.fetch_next_nonce()
    client_order_index = random.randint(1 << 20, 1 << 28)
    order_expiry = 0 if scenario.time_in_force == 0 else -1
    tx_info, err = await context.signer.sign_create_order_tx(
        market_index=market_index,
        client_order_index=client_order_index,
        base_amount=base_amount,
        price=price,
        is_ask=scenario.is_ask,
        order_type=scenario.order_type,
        time_in_force=scenario.time_in_force,
        reduce_only=scenario.reduce_only,
        trigger_price=trigger_raw or 0,
        order_expiry=order_expiry,
        nonce=nonce,
    )
    if err:
        return ScenarioResult(
            scenario=scenario,
            nonce=nonce,
            client_order_index=client_order_index,
            price_used=price,
            trigger_used=trigger_raw or 0,
            tx_hash=None,
            error=str(err),
        )
    try:
        response = await context.http_tx.send_tx(
            tx_type=context.signer.get_tx_type_create_order(),
            tx_info=tx_info,
        )
        tx_hash = str(response.get("tx_hash"))
        logger.info(
            "scenario %s submitted tx_hash=%s price=%s tif=%s reduce_only=%s",
            scenario.name,
            tx_hash,
            price,
            scenario.time_in_force,
            scenario.reduce_only,
        )
        return ScenarioResult(
            scenario=scenario,
            nonce=nonce,
            client_order_index=client_order_index,
            price_used=price,
            trigger_used=trigger_raw or 0,
            tx_hash=tx_hash,
            error=None,
        )
    except Exception as exc:
        logger.warning("scenario %s send_tx failed: %s", scenario.name, exc)
        return ScenarioResult(
            scenario=scenario,
            nonce=nonce,
            client_order_index=client_order_index,
            price_used=price,
            trigger_used=trigger_raw or 0,
            tx_hash=None,
            error=str(exc),
        )


async def _run_batch(
    context: LighterTestContext,
    *,
    market_index: int,
    base_prices: dict[str, int],
    base_amount: int,
) -> dict[str, Any]:
    buy_anchor = min(base_prices["mark"], base_prices["bid"])
    sell_anchor = max(base_prices["mark"], base_prices["ask"])
    buy_price = await context.clamp_price(
        market_index,
        _scale_price(buy_anchor, 0.001, is_ask=False),
        is_ask=False,
    )
    sell_price = await context.clamp_price(
        market_index,
        _scale_price(sell_anchor, 0.001, is_ask=True),
        is_ask=True,
    )
    base_nonce = await context.fetch_next_nonce()
    payloads: list[str] = []
    tx_types: list[int] = []
    for idx, (is_ask, price, tif, reduce_only) in enumerate(
        [
            (False, buy_price, 1, False),
            (True, sell_price, 1, True),
        ]
    ):
        tx_info, err = await context.signer.sign_create_order_tx(
            market_index=market_index,
            client_order_index=random.randint(1 << 20, 1 << 28),
            base_amount=base_amount,
            price=price,
            is_ask=is_ask,
            order_type=0,
            time_in_force=tif,
            reduce_only=reduce_only,
            trigger_price=0,
            order_expiry=-1,
            nonce=base_nonce + idx,
        )
        if err or not tx_info:
            raise RuntimeError(f"batch sign error: {err}")
        payloads.append(tx_info)
        tx_types.append(context.signer.get_tx_type_create_order())
    response = await context.http_tx.send_tx_batch(
        tx_types=tx_types,
        tx_infos=payloads,
    )
    logger.info("sendTxBatch response=%s", response)
    return response


async def _nonce_conflict_probe(
    context: LighterTestContext,
    *,
    market_index: int,
    base_prices: dict[str, int],
    base_amount: int,
) -> str | None:
    try:
        price = await context.clamp_price(
            market_index,
            _scale_price(min(base_prices["mark"], base_prices["bid"]), 0.0005, is_ask=False),
            is_ask=False,
        )
        nonce = await context.fetch_next_nonce()
        coid = random.randint(1 << 20, 1 << 28)
        tx_info, err = await context.signer.sign_create_order_tx(
            market_index=market_index,
            client_order_index=coid,
            base_amount=base_amount,
            price=price,
            is_ask=False,
            order_type=0,
            time_in_force=1,
            reduce_only=False,
            trigger_price=0,
            order_expiry=-1,
            nonce=nonce,
        )
        if err or not tx_info:
            return f"sign error: {err}"
        await context.http_tx.send_tx(
            tx_type=context.signer.get_tx_type_create_order(),
            tx_info=tx_info,
        )
    except Exception as exc:
        return str(exc)
    try:
        await context.http_tx.send_tx(
            tx_type=context.signer.get_tx_type_create_order(),
            tx_info=tx_info,
        )
    except Exception as exc:
        logger.info("nonce conflict probe expected failure: %s", exc)
        return str(exc)
    return "unexpected success"


async def _cancel_all(context: LighterTestContext) -> str | None:
    nonce = await context.fetch_next_nonce()
    from nautilus_trader.adapters.lighter.common.enums import LighterCancelAllTif
    tx_info, err = await context.signer.sign_cancel_all_orders_tx(time_in_force=LighterCancelAllTif.IMMEDIATE, time=0, nonce=nonce)
    if err or not tx_info:
        logger.warning("cancel_all sign error: %s", err)
        return None
    response = await context.http_tx.send_tx(
        tx_type=context.signer.get_tx_type_cancel_all(),
        tx_info=tx_info,
    )
    tx_hash = str(response.get("tx_hash"))
    logger.info("cancel_all tx_hash=%s", tx_hash)
    return tx_hash


async def run_suite(
    *,
    credentials_path: Path,
    base_http: str,
    base_ws: str | None,
    market_index: int,
    base_amount: int,
) -> None:
    context = LighterTestContext(
        credentials_path=credentials_path,
        base_http=base_http,
        base_ws=base_ws,
    )
    try:
        token = context.auth_token()
        account = await context.http_account.get_account_overview(
            account_index=context.credentials.account_index,
            auth=token,
        )
        logger.info("account overview keys=%s", list(account.keys()))

        base_prices = await _baseline_prices(context, market_index)

        scenarios = [
            OrderScenario("limit_gtc_buy", order_type=0, time_in_force=1, is_ask=False, reduce_only=False, price_offset=0.0005),
            OrderScenario("limit_ioc_buy", order_type=0, time_in_force=0, is_ask=False, reduce_only=False, price_offset=0.0002),
            OrderScenario("limit_gtc_reduce_only_sell", order_type=0, time_in_force=1, is_ask=True, reduce_only=True, price_offset=0.0005),
            OrderScenario("limit_ioc_reduce_only_sell", order_type=0, time_in_force=0, is_ask=True, reduce_only=True, price_offset=0.0007),
            OrderScenario("market_ioc_buy", order_type=1, time_in_force=0, is_ask=False, reduce_only=False, price_offset=0.0),
            OrderScenario("limit_post_only_buy", order_type=0, time_in_force=2, is_ask=False, reduce_only=False, price_offset=0.0008),
            OrderScenario("limit_post_only_reduce_only_sell", order_type=0, time_in_force=2, is_ask=True, reduce_only=True, price_offset=0.0008),
            OrderScenario("market_ioc_reduce_only_sell", order_type=1, time_in_force=0, is_ask=True, reduce_only=True, price_offset=0.0),
        ]
        results = []
        for scenario in scenarios:
            result = await _submit_order(
                context,
                market_index=market_index,
                base_prices=base_prices,
                scenario=scenario,
                base_amount=base_amount,
            )
            results.append(result)

        batch_resp: dict[str, Any] | None = None
        try:
            batch_resp = await _run_batch(
                context,
                market_index=market_index,
                base_prices=base_prices,
                base_amount=base_amount,
            )
        except Exception as exc:
            logger.warning("sendTxBatch failed: %s", exc)

        conflict = await _nonce_conflict_probe(
            context,
            market_index=market_index,
            base_prices=base_prices,
            base_amount=base_amount,
        )

        cancel_hash = await _cancel_all(context)

        txs = await context.http_account.get_account_txs(
            account_index=context.credentials.account_index,
            auth=token,
            limit=25,
        )
        logger.info("account_txs keys=%s", list(txs.keys()) if isinstance(txs, dict) else type(txs))

        summary = {
            "scenarios": [
                {
                    "name": res.scenario.name,
                    "tx_hash": res.tx_hash,
                    "error": res.error,
                    "nonce": res.nonce,
                    "client_order_index": res.client_order_index,
                    "price": res.price_used,
                    "trigger": res.trigger_used,
                }
                for res in results
            ],
            "batch_response": batch_resp,
            "nonce_conflict": conflict,
            "cancel_all_tx_hash": cancel_hash,
        }
        logger.info("regression summary=%s", summary)
    finally:
        await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lighter adapter regression matrix")
    parser.add_argument("--credentials", default=os.getenv("LIGHTER_CREDENTIALS_JSON", "secrets/lighter_testnet_account.json"))
    parser.add_argument("--http", default=os.getenv("LIGHTER_HTTP", "https://testnet.zklighter.elliot.ai"))
    parser.add_argument("--ws", default=os.getenv("LIGHTER_WS"))
    parser.add_argument("--market-index", type=int, default=int(os.getenv("LIGHTER_MARKET_INDEX", "1")))
    parser.add_argument("--base-amount", type=int, default=int(os.getenv("LIGHTER_BASE_AMOUNT", "20")))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    configure_logging(verbose=not args.quiet)
    asyncio.run(
        run_suite(
            credentials_path=Path(args.credentials),
            base_http=args.http,
            base_ws=args.ws,
            market_index=args.market_index,
            base_amount=args.base_amount,
        )
    )


if __name__ == "__main__":
    main()
