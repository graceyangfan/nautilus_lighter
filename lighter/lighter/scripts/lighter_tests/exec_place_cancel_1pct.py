#!/usr/bin/env python3
from __future__ import annotations

"""
Place + cancel smoke test for ``LighterExecutionClient``.

Behavior
--------
- Loads BNB perpetual on Lighter.
- Places a post-only LIMIT BUY order ~1.0% below best ask (or best bid) to avoid fills.
- Waits for OrderAccepted and venue_order_id mapping.
- Sends a CancelOrder for that single order (or CancelAllOrders as a fallback).
- Records private WS and HTTP traffic when the respective env vars are set.

Environment (defaults target mainnet unless overridden)
------------------------------------------------------
  LIGHTER_HTTP_URL   = https://mainnet.zklighter.elliot.ai
  LIGHTER_WS_URL     = wss://mainnet.zklighter.elliot.ai/stream
  LIGHTER_PUBLIC_KEY = ...
  LIGHTER_PRIVATE_KEY= ...
  LIGHTER_ACCOUNT_INDEX = ...
  LIGHTER_API_KEY_INDEX = ...

Optional recording:
  LIGHTER_EXEC_RECORD = /tmp/nautilus_exec_ws_place_cancel_1pct.jsonl
  LIGHTER_HTTP_RECORD = /tmp/nautilus_http_place_cancel_1pct.jsonl
"""

import asyncio
import os
from decimal import Decimal, ROUND_FLOOR

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelAllOrders, CancelOrder, SubmitOrder
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import StrategyId, TraderId
from nautilus_trader.model.objects import Price, Quantity


async def main() -> int:
    http_url = os.getenv("LIGHTER_HTTP_URL", "https://mainnet.zklighter.elliot.ai").rstrip("/")
    ws_url = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")

    priv = os.getenv("LIGHTER_PRIVATE_KEY")
    pub = os.getenv("LIGHTER_PUBLIC_KEY")
    if not priv or not pub:
        print("ERROR: LIGHTER_PUBLIC_KEY / LIGHTER_PRIVATE_KEY not set")
        return 1
    acct = int(os.getenv("LIGHTER_ACCOUNT_INDEX", "XXXXX"))
    api_idx = int(os.getenv("LIGHTER_API_KEY_INDEX", "2"))

    # Provide default record paths if not already configured.
    os.environ.setdefault("LIGHTER_EXEC_RECORD", "/tmp/nautilus_exec_ws_place_cancel_1pct.jsonl")
    os.environ.setdefault("LIGHTER_HTTP_RECORD", "/tmp/nautilus_http_place_cancel_1pct.jsonl")

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("NAUT-PLACE-CANCEL")
    strat = StrategyId("NAUT-PLACE-CANCEL")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    http_pub = LighterPublicHttpClient(base_url=http_url)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async(filters={"bases": ["BNB"]})
    if not provider.get_all():
        print("ERROR: no BNB instruments loaded; aborting")
        return 1
    inst_id, inst = next(iter(provider.get_all().items()))
    cache.add_instrument(inst)

    creds = LighterCredentials(
        pubkey=pub,
        account_index=acct,
        api_key_index=api_idx,
        private_key=priv,
    )

    exec_client = LighterExecutionClient(
        loop=loop,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,
        base_url_http=http_url,
        base_url_ws=ws_url,
        credentials=creds,
        config=LighterExecClientConfig(
            base_url_http=http_url,
            base_url_ws=ws_url,
            credentials=creds,
            chain_id=304,  # mainnet
            subscribe_account_stats=False,
            post_submit_http_reconcile=False,
        ),
    )

    # Simple order lifecycle event logger.
    def on_event(evt: object) -> None:
        name = type(evt).__name__
        if name in {
            "OrderSubmitted",
            "OrderAccepted",
            "OrderUpdated",
            "OrderCanceled",
            "OrderRejected",
            "OrderModifyRejected",
        }:
            parts = [name]
            for attr in ("client_order_id", "venue_order_id", "order_status", "reason"):
                if hasattr(evt, attr):
                    parts.append(f"{attr}={getattr(evt, attr)}")
            print("[EVENT]", " ".join(parts))

    msgbus.register(endpoint="ExecEngine.process", handler=on_event)

    # Connect execution client and wait briefly for private subscriptions.
    await exec_client._connect()  # type: ignore[protected-access]
    await asyncio.sleep(2.0)

    # Derive a 1% off-top price from public order book (prefer best ask for BUY).
    mkt_id = int(inst.info.get("market_id"))
    book = await http_pub.get_order_book_orders(mkt_id, limit=5)
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = Decimal(str(bids[0]["price"])) if bids else None
    best_ask = Decimal(str(asks[0]["price"])) if asks else None

    tick = Decimal(str(inst.price_increment))
    if best_ask is not None:
        target_px = best_ask * Decimal("0.99")  # 1% below best ask
    elif best_bid is not None:
        target_px = best_bid * Decimal("0.99")
    else:
        target_px = Decimal("100")

    if tick > 0:
        steps = (target_px / tick).to_integral_value(rounding=ROUND_FLOOR)
        target_px = steps * tick

    qty_dec = Decimal("0.02")
    notional = target_px * qty_dec
    min_notional = Decimal(str(inst.min_notional.as_decimal()))
    print(f"[PARAMS] price={target_px} qty={qty_dec} notional={notional} min_notional={min_notional}")
    if notional < min_notional:
        print("ERROR: notional below min_notional; aborting")
        await exec_client._disconnect()  # type: ignore[protected-access]
        return 1

    of = OrderFactory(trader, strat, clock)
    order = of.limit(
        instrument_id=inst_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str(str(qty_dec)),
        price=Price.from_str(str(target_px)),
        time_in_force=TimeInForce.GTC,
        post_only=True,  # avoid crossing book; order should rest and not fill
    )
    cache.add_order(order, None)

    submit_cmd = SubmitOrder(
        trader_id=trader,
        strategy_id=strat,
        position_id=None,
        order=order,
        command_id=UUID4(),
        ts_init=clock.timestamp_ns(),
    )
    print("[TEST] submitting order...")
    exec_client.submit_order(submit_cmd)

    coid_str = order.client_order_id.value
    venue_oid = None

    # Wait for venue mapping from private WS (account_all_orders / account_all).
    for _ in range(60):
        await asyncio.sleep(0.5)
        venue = exec_client._venue_order_id_by_coid.get(coid_str)  # type: ignore[attr-defined]
        if venue is not None:
            venue_oid = venue
            break

    if venue_oid is None:
        print("[TEST] no venue_order_id mapping found; sending CancelAllOrders as fallback...")
        cancel_all = CancelAllOrders(
            trader_id=trader,
            strategy_id=strat,
            instrument_id=inst_id,
            order_side=OrderSide.NO_ORDER_SIDE,
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        )
        exec_client.cancel_all_orders(cancel_all)
    else:
        print(f"[TEST] mapped venue_order_id={venue_oid}, sending CancelOrder...")
        cancel_cmd = CancelOrder(
            trader_id=trader,
            strategy_id=strat,
            instrument_id=inst_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_oid,
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        )
        exec_client.cancel_order(cancel_cmd)

    # Allow time for cancel events and final position/account updates.
    await asyncio.sleep(20.0)
    await exec_client._disconnect()  # type: ignore[protected-access]
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
