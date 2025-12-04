#!/usr/bin/env python3
from __future__ import annotations

"""
Lightweight trade-path tests for LighterExecutionClient.

This script exercises `_process_account_trades` using a synthetic
`LighterAccountTradeStrict` and verifies that:

1) A trade for one of our orders produces exactly one `FillReport`.
2) The same trade produces exactly one `OrderFilled`.
3) `OrderFilled.order_type` is taken from the cached order (LIMIT),
   not hard-coded.

Usage:

  PYTHONPATH=/root/myenv/lib/python3.12/site-packages \\
  python adapters/lighter/scripts/lighter_tests/test_execution_trades.py
"""

import sys
from collections import Counter

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.schemas.ws import LighterAccountTradeStrict
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
from nautilus_trader.model.identifiers import TraderId, StrategyId, InstrumentId, ClientOrderId
from nautilus_trader.model.objects import Price, Quantity, Currency
from nautilus_trader.execution.reports import FillReport


class _DummyInstrument:
    """Minimal instrument stub for trade-path tests."""

    def __init__(self, inst_id: InstrumentId, quote_ccy: Currency) -> None:
        self.id = inst_id
        self.quote_currency = quote_ccy

    def make_qty(self, size: str | float | int) -> Quantity:
        return Quantity.from_str(str(size))

    def make_price(self, price: str | float | int) -> Price:
        return Price.from_str(str(price))


def _build_offline_exec_client() -> LighterExecutionClient:
    """Construct a LighterExecutionClient suitable for offline trade tests."""
    clock = LiveClock()
    trader = TraderId("TRADER-UNIT")
    strat = StrategyId("STRAT-UNIT")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    creds = LighterCredentials(
        pubkey="0x" + "11" * 20,
        account_index=1,
        api_key_index=1,
        private_key="0x" + "22" * 40,
    )

    cfg = LighterExecClientConfig(
        base_url_http="https://testnet.zklighter.elliot.ai",
        base_url_ws="wss://testnet.zklighter.elliot.ai/stream",
        credentials=creds,
        chain_id=300,
        subscribe_account_stats=False,
        post_submit_http_reconcile=False,
    )

    exec_client = LighterExecutionClient(
        loop=clock._loop,  # type: ignore[attr-defined]
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=None,  # type: ignore[arg-type]
        base_url_http=cfg.base_url_http or "",
        base_url_ws=cfg.base_url_ws or "",
        credentials=creds,
        config=cfg,
    )
    return exec_client


def _exercise_trade_path() -> None:
    exec_client = _build_offline_exec_client()
    clock = exec_client._clock  # type: ignore[attr-defined]
    msgbus = exec_client._msgbus  # type: ignore[attr-defined]
    cache = exec_client._cache  # type: ignore[attr-defined]

    events: list[object] = []

    def on_event(evt: object) -> None:
        events.append(evt)

    msgbus.register(endpoint="ExecEngine.process", handler=on_event)

    # Prepare dummy instrument + mapping
    inst_id = InstrumentId.from_str("BNBUSDC-PERP.LIGHTER")
    quote_ccy = Currency.from_str("USDC")
    dummy_inst = _DummyInstrument(inst_id, quote_ccy)
    cache.add_instrument(dummy_inst)

    # Create a LIMIT order in cache
    trader = msgbus.trader_id
    strat = StrategyId("STRAT-UNIT")
    from nautilus_trader.common.factories import OrderFactory

    of = OrderFactory(trader, strat, clock)
    order = of.limit(
        instrument_id=inst_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str("0.01"),
        price=Price.from_str("100.00"),
        time_in_force=TimeInForce.GTC,
        post_only=True,
    )
    cache.add_order(order, None)

    coid: ClientOrderId = order.client_order_id
    venue_order_index = 123456789

    # Wire mappings required by _process_account_trades
    exec_client._coid_by_venue_order_index[venue_order_index] = coid  # type: ignore[attr-defined]
    cache.add_venue_order_id(coid, VenueOrderId=str(venue_order_index))  # type: ignore[arg-type]

    # Synthetic trade where our order is the bid side; we are maker.
    tr = LighterAccountTradeStrict(
        trade_id=1,
        market_id=25,
        price="100.00",
        size="0.01",
        ask_id=venue_order_index + 1,
        bid_id=venue_order_index,
        ask_account_id=999,
        bid_account_id=1,
        is_maker_ask=False,  # bid is maker
        block_height=1,
        timestamp=clock.timestamp_ns() // 1_000_000,
    )

    exec_client._process_account_trades([tr])  # type: ignore[attr-defined]

    names = [type(e).__name__ for e in events]
    counts = Counter(names)
    print("[TRADES] events:", counts)

    # We expect exactly one FillReport and one OrderFilled.
    assert counts.get("FillReport", 0) == 1, "expected exactly one FillReport"
    assert counts.get("OrderFilled", 0) == 1, "expected exactly one OrderFilled"

    # Verify order_type for OrderFilled is LIMIT, not hard-coded.
    filled = [e for e in events if type(e).__name__ == "OrderFilled"]
    assert len(filled) == 1
    of_event = filled[0]
    assert getattr(of_event, "order_type", None) == OrderType.LIMIT, "OrderFilled.order_type must be LIMIT"

    # Basic sanity: quantities and prices match the trade.
    assert str(of_event.last_qty) == "0.01"
    assert str(of_event.last_px) == "100.00"

    print("[OK] LighterExecutionClient trade path produces FillReport + OrderFilled with correct order_type")


def main() -> None:
    try:
        _exercise_trade_path()
    except AssertionError as exc:
        print("[FAIL]", str(exc))
        sys.exit(1)
    except RuntimeError as exc:
        msg = str(exc)
        if "lighter-go signer library not available" in msg:
            print("[SKIP] signer library unavailable; execution trade test skipped")
            sys.exit(0)
        raise
    print("[DONE] test_execution_trades")


if __name__ == "__main__":
    main()
