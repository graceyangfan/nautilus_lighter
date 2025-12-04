#!/usr/bin/env python3
from __future__ import annotations

"""
Exercise LighterExecutionClient batch place + batch cancel on a single market.

Goal:
  - Use `_submit_order_list` → transport `submit_order_list` → `jsonapi/sendtxbatch`
    to place two far-from-market post-only LIMIT orders (to avoid fills).
  - Use `_batch_cancel_orders` → transport `cancel_orders` → `jsonapi/sendtxbatch`
    to cancel both orders in a single batch.
  - Print key order events (OrderSubmitted / OrderAccepted / OrderCanceled) so
    behaviour can be cross-checked against the recorded private WS JSONL.

Notes:
  - Only uses Nautilus `LighterExecutionClient`; the official lighter-python SDK
    is never imported on the live path.
  - Uses small BNB orders far away from the best bid to minimise fill risk.
  - Works together with `LIGHTER_EXEC_RECORD` so private WS traffic can be
    analysed by `analyze_exec_ws_jsonl.py`.

Environment (defaults target mainnet unless overridden):
  LIGHTER_HTTP_URL=https://mainnet.zklighter.elliot.ai
  LIGHTER_WS_URL=wss://mainnet.zklighter.elliot.ai/stream
  LIGHTER_PUBLIC_KEY=...
  LIGHTER_PRIVATE_KEY=...
  LIGHTER_ACCOUNT_INDEX=...
  LIGHTER_API_KEY_INDEX=...
  # optional, used by LighterExecutionClient for raw WS recording
  LIGHTER_EXEC_RECORD=/tmp/nautilus_exec_ws_batch_place_cancel.jsonl

Example:
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages:~/nautilus_trader-develop \\
  python scripts/lighter_tests/exec_batch_place_cancel.py
"""

import asyncio
import os
from collections import Counter
from decimal import Decimal
from typing import List

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, TraderId, StrategyId, VenueOrderId
from nautilus_trader.model.objects import Price, Quantity


class _DummyOrderList:
    """Minimal OrderList-like wrapper exposing `.orders` as expected by ExecClient."""

    def __init__(self, orders: List[object]) -> None:
        self.orders = list(orders)


class _DummySubmitOrderList:
    """Minimal SubmitOrderList-like command for driving `_submit_order_list`."""

    def __init__(self, trader_id: TraderId, strategy_id: StrategyId, order_list: _DummyOrderList) -> None:
        self.trader_id = trader_id
        self.strategy_id = strategy_id
        self.order_list = order_list
        # Unused by `_submit_order_list`, kept for shape compatibility
        self.position_id = None
        self.client_id = None
        self.params: dict[str, object] = {}


class _DummyCancelItem:
    """Single cancel item used inside DummyBatchCancelOrders."""

    def __init__(self, instrument_id: InstrumentId, venue_order_id: VenueOrderId) -> None:
        self.instrument_id = instrument_id
        self.venue_order_id = venue_order_id


class _DummyBatchCancelOrders:
    """Minimal BatchCancelOrders-like command for driving `_batch_cancel_orders`."""

    def __init__(self, trader_id: TraderId, strategy_id: StrategyId, cancels: List[_DummyCancelItem]) -> None:
        self.trader_id = trader_id
        self.strategy_id = strategy_id
        self.cancels = list(cancels)


async def main() -> int:
    http_url = os.getenv("LIGHTER_HTTP_URL", "https://mainnet.zklighter.elliot.ai").rstrip("/")
    ws_url = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")

    print(f"[BATCH] HTTP={http_url}")
    print(f"[BATCH] WS={ws_url}")
    print(f"[BATCH] LIGHTER_EXEC_RECORD={os.getenv('LIGHTER_EXEC_RECORD')}")

    priv = os.getenv("LIGHTER_PRIVATE_KEY")
    pub = os.getenv("LIGHTER_PUBLIC_KEY")
    if not priv or not pub:
        print("[BATCH] ERROR: LIGHTER_PUBLIC_KEY / LIGHTER_PRIVATE_KEY not set")
        return 1
    acct = int(os.getenv("LIGHTER_ACCOUNT_INDEX", "XXXXX"))
    api_idx = int(os.getenv("LIGHTER_API_KEY_INDEX", "2"))

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("NAUT-BATCH")
    strat = StrategyId("NAUT-BATCH")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    http_pub = LighterPublicHttpClient(base_url=http_url)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async(filters={"bases": ["BNB"]})
    instruments = provider.get_all()
    if not instruments:
        print("[BATCH] ERROR: no BNB market found")
        return 1
    inst_id, inst = next(iter(instruments.items()))
    cache.add_instrument(inst)
    market_index = int(inst.info.get("market_id"))
    print(f"[BATCH] using instrument={inst_id} (market_id={market_index})")

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

    events: list[tuple[str, object]] = []

    def on_event(evt: object) -> None:
        name = type(evt).__name__
        events.append((name, evt))
        if name in {"OrderSubmitted", "OrderAccepted", "OrderUpdated", "OrderCanceled", "OrderRejected"}:
            line = [name]
            for attr in ("client_order_id", "venue_order_id", "order_status", "reason"):
                if hasattr(evt, attr):
                    line.append(f"{attr}={getattr(evt, attr)}")
            print("[EVENT]", " ".join(line))

    msgbus.register(endpoint="ExecEngine.process", handler=on_event)

    print("[BATCH] connecting ExecutionClient...")
    await exec_client._connect()
    await asyncio.sleep(3)
    print("[BATCH] connected")

    # 1) Fetch book and derive far-from-market BUY prices to minimise fill risk
    print("[BATCH] fetching BNB order book for pricing...")
    ob = await http_pub.get_order_book_orders(market_index, limit=5)
    bids = ob.get("bids") or []
    asks = ob.get("asks") or []
    best_bid = Decimal(str(bids[0]["price"])) if bids else Decimal("800")
    best_ask = Decimal(str(asks[0]["price"])) if asks else Decimal("801")
    mid = (best_bid + best_ask) / 2
    print(f"[BATCH] mid={mid:.4f} bid={best_bid} ask={best_ask}")

    tick = Decimal(str(inst.price_increment))
    # Prices ~8% / 9% below bid, post-only, should not fill in normal conditions.
    price1_dec = (best_bid * Decimal("0.92")).quantize(tick)
    price2_dec = (best_bid * Decimal("0.91")).quantize(tick)
    qty_dec = Decimal("0.02")

    print(f"[BATCH] order1: price={price1_dec} (≈{(price1_dec / best_bid - 1) * 100:+.2f}% vs bid) qty={qty_dec}")
    print(f"[BATCH] order2: price={price2_dec} (≈{(price2_dec / best_bid - 1) * 100:+.2f}% vs bid) qty={qty_dec}")

    of = OrderFactory(trader, strat, exec_client._clock)  # type: ignore[attr-defined]
    order1 = of.limit(
        instrument_id=inst_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str(str(qty_dec)),
        price=Price.from_str(str(price1_dec)),
        time_in_force=TimeInForce.GTC,
        post_only=True,
    )
    order2 = of.limit(
        instrument_id=inst_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str(str(qty_dec)),
        price=Price.from_str(str(price2_dec)),
        time_in_force=TimeInForce.GTC,
        post_only=True,
    )
    cache.add_order(order1, None)
    cache.add_order(order2, None)
    print(f"[BATCH] order1 coid={order1.client_order_id}")
    print(f"[BATCH] order2 coid={order2.client_order_id}")

    # 2) Build DummySubmitOrderList and call `_submit_order_list` (true batch create)
    order_list = _DummyOrderList([order1, order2])
    sol_cmd = _DummySubmitOrderList(trader, strat, order_list)

    print("[BATCH] sending SubmitOrderList (batch create)...")
    await exec_client._submit_order_list(sol_cmd)
    print("[BATCH] SubmitOrderList sent, waiting 10s for private WS updates...")
    await asyncio.sleep(10)

    # 3) Resolve venue_order_id mapping from ExecutionClient internals
    print("[BATCH] resolving venue_order_id mappings from ExecutionClient...")
    coid1 = order1.client_order_id
    coid2 = order2.client_order_id
    venue1: VenueOrderId | None = None
    venue2: VenueOrderId | None = None
    for _ in range(40):
        await asyncio.sleep(0.5)
        mapping = exec_client._venue_order_id_by_coid  # type: ignore[attr-defined]
        if coid1.value in mapping and venue1 is None:
            venue1 = mapping[coid1.value]
            print(f"[BATCH] mapping: {coid1} -> {venue1}")
        if coid2.value in mapping and venue2 is None:
            venue2 = mapping[coid2.value]
            print(f"[BATCH] mapping: {coid2} -> {venue2}")
        if venue1 is not None and venue2 is not None:
            break
    if venue1 is None or venue2 is None:
        print("[BATCH] WARN: did not resolve all venue_order_id mappings in time; still attempting batch cancel")

    # 4) Build DummyBatchCancelOrders and call `_batch_cancel_orders` (true batch cancel)
    cancels: list[_DummyCancelItem] = []
    if venue1 is not None:
        cancels.append(_DummyCancelItem(inst_id, venue1))
    if venue2 is not None:
        cancels.append(_DummyCancelItem(inst_id, venue2))
    if not cancels:
        print("[BATCH] no valid venue_order_id, skipping batch cancel")
    else:
        bc_cmd = _DummyBatchCancelOrders(trader, strat, cancels)
        print("[BATCH] sending BatchCancelOrders (batch cancel)...")
        await exec_client._batch_cancel_orders(bc_cmd)
        print("[BATCH] BatchCancelOrders sent, waiting 10s for canceled states...")
        await asyncio.sleep(10)

    print("[BATCH] disconnecting ExecutionClient...")
    await exec_client._disconnect()

    counter = Counter(name for name, _ in events)
    print("\n[BATCH] event counts:")
    for name, cnt in sorted(counter.items()):
        print(f"  {name}: {cnt}")

    print("\n[BATCH] order-related events:")
    for name, evt in events:
        if name in {"OrderSubmitted", "OrderAccepted", "OrderUpdated", "OrderCanceled", "OrderRejected"}:
            line = [name]
            for attr in ("client_order_id", "venue_order_id", "order_status", "reason"):
                if hasattr(evt, attr):
                    line.append(f"{attr}={getattr(evt, attr)}")
            print("  - " + " ".join(line))

    print(
        "\n[BATCH] done. If LIGHTER_EXEC_RECORD is set, inspect the JSONL for "
        "jsonapi/sendtxbatch results and account_all_orders snapshots."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
