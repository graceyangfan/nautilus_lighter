#!/usr/bin/env python3
from __future__ import annotations

"""
Exercise three ModifyOrder scenarios against LighterExecutionClient on a single market:

1) Price-only modify  (quantity unchanged)
2) Quantity-only modify (price unchanged)
3) Modify both price and quantity

For each case:
  - Submit a far-from-market post-only LIMIT BUY to avoid fills.
  - Wait for OrderAccepted / venue mapping.
  - Send the corresponding ModifyOrder.
  - Optionally record private WS to JSONL via LIGHTER_EXEC_RECORD.

Environment (defaults target mainnet unless overridden):
  LIGHTER_HTTP_URL=https://mainnet.zklighter.elliot.ai
  LIGHTER_WS_URL=wss://mainnet.zklighter.elliot.ai/stream
  LIGHTER_PUBLIC_KEY=...
  LIGHTER_PRIVATE_KEY=...
  LIGHTER_ACCOUNT_INDEX=...
  LIGHTER_API_KEY_INDEX=...
  LIGHTER_EXEC_RECORD=/tmp/lighter_exec_modify_cases.jsonl  (optional)
"""

import asyncio
import os
from decimal import Decimal

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import SubmitOrder, ModifyOrder, CancelAllOrders
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import TraderId, StrategyId
from nautilus_trader.model.objects import Price, Quantity


async def _run_case(
    exec_client: LighterExecutionClient,
    http_pub: LighterPublicHttpClient,
    cache: Cache,
    trader: TraderId,
    strat: StrategyId,
    case_name: str,
) -> None:
    # Use the first BNB market from provider
    provider = exec_client._instrument_provider  # type: ignore[attr-defined]
    instruments = provider.get_all()
    inst_id, inst = next(iter(instruments.items()))
    cache.add_instrument(inst)

    # Fetch best bid/ask and derive far-from-market prices
    mkt_id = int(inst.info.get("market_id"))
    doc = await http_pub.get_order_book_orders(mkt_id, limit=5)
    bids = doc.get("bids") or []
    asks = doc.get("asks") or []
    best_bid = Decimal(str(bids[0]["price"])) if bids else Decimal("800")
    best_ask = Decimal(str(asks[0]["price"])) if asks else Decimal("801")

    tick = Decimal(str(inst.price_increment))
    create_price_dec = (best_bid * Decimal("0.985")).quantize(tick)
    create_qty_dec = Decimal("0.02")

    if case_name == "CASE_PRICE_ONLY":
        modify_price_dec = (best_bid * Decimal("0.99")).quantize(tick)
        modify_qty_dec = None
    elif case_name == "CASE_QTY_ONLY":
        modify_price_dec = None
        modify_qty_dec = Decimal("0.03")
    else:  # CASE_BOTH
        modify_price_dec = (best_bid * Decimal("0.99")).quantize(tick)
        modify_qty_dec = Decimal("0.03")

    of = OrderFactory(trader, strat, exec_client._clock)  # type: ignore[attr-defined]
    order = of.limit(
        instrument_id=inst_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str(str(create_qty_dec)),
        price=Price.from_str(str(create_price_dec)),
        time_in_force=TimeInForce.GTC,
        post_only=True,
    )
    cache.add_order(order, None)

    submit_cmd = SubmitOrder(
        trader_id=trader,
        strategy_id=strat,
        position_id=None,
        order=order,
        command_id=UUID4(),
        ts_init=exec_client._clock.timestamp_ns(),  # type: ignore[attr-defined]
    )
    await exec_client._submit_order(submit_cmd)

    # Wait for OrderAccepted / mapping
    coid_str = order.client_order_id.value
    for _ in range(60):
        await asyncio.sleep(0.5)
        if coid_str in exec_client._accepted_coids:  # type: ignore[attr-defined]
            break
        if coid_str in exec_client._venue_order_id_by_coid:  # type: ignore[attr-defined]
            break

    new_qty_obj = Quantity.from_str(str(modify_qty_dec)) if modify_qty_dec is not None else None
    new_price_obj = Price.from_str(str(modify_price_dec)) if modify_price_dec is not None else None

    modify_cmd = ModifyOrder(
        trader_id=trader,
        strategy_id=strat,
        instrument_id=inst_id,
        client_order_id=order.client_order_id,
        venue_order_id=None,
        quantity=new_qty_obj,
        price=new_price_obj,
        trigger_price=None,
        command_id=UUID4(),
        ts_init=exec_client._clock.timestamp_ns(),  # type: ignore[attr-defined]
    )

    await exec_client._modify_order(modify_cmd)
    # Allow some time for private WS updates
    await asyncio.sleep(15)

    cancel_cmd = CancelAllOrders(
        trader_id=trader,
        strategy_id=strat,
        instrument_id=inst_id,
        order_side=OrderSide.NO_ORDER_SIDE,
        command_id=UUID4(),
        ts_init=exec_client._clock.timestamp_ns(),  # type: ignore[attr-defined]
    )
    await exec_client._cancel_all_orders(cancel_cmd)
    await asyncio.sleep(10)


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

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("NAUT-MODCASES")
    strat = StrategyId("NAUT-MODCASES")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    http_pub = LighterPublicHttpClient(base_url=http_url)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async(filters={"bases": ["BNB"]})

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
            chain_id=304,
            subscribe_account_stats=False,
            post_submit_http_reconcile=True,
        ),
    )

    # Simple event printout
    from collections import Counter

    events: list[tuple[str, object]] = []

    def on_event(evt: object) -> None:
        name = type(evt).__name__
        events.append((name, evt))
        if name in {"OrderSubmitted", "OrderAccepted", "OrderUpdated", "OrderCanceled", "OrderModifyRejected"}:
            line = [name]
            for attr in ("client_order_id", "venue_order_id", "order_status", "reason"):
                if hasattr(evt, attr):
                    line.append(f"{attr}={getattr(evt, attr)}")
            print("[EVENT]", " ".join(line))

    msgbus.register(endpoint="ExecEngine.process", handler=on_event)

    await exec_client._connect()
    await asyncio.sleep(3)

    for case_name in ("CASE_PRICE_ONLY", "CASE_QTY_ONLY", "CASE_BOTH"):
        await _run_case(exec_client, http_pub, cache, trader, strat, case_name)

    await exec_client._disconnect()

    counter = Counter(name for name, _ in events)
    print("\n[MODCASES] Event counts:")
    for name, cnt in sorted(counter.items()):
        print(f"  {name}: {cnt}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
