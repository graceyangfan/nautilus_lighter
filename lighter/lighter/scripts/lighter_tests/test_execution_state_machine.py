#!/usr/bin/env python3
from __future__ import annotations

"""
Lightweight state-machine tests for LighterExecutionClient.

This script exercises the private WS order-processing path using synthetic
`LighterAccountOrderStrict` objects, without hitting the real network. It
verifies that:

1) First `status=open` frame for an order emits `OrderAccepted`.
2) Subsequent `status=open` frame with changed price/quantity emits `OrderUpdated`.
3) A later `status=canceled` frame emits `OrderCanceled` once.

Usage (from the installed environment):

  PYTHONPATH=/root/myenv/lib/python3.12/site-packages \\
  python adapters/lighter/scripts/lighter_tests/test_execution_state_machine.py
"""

import sys
from collections import Counter

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.schemas.ws import LighterAccountOrderStrict
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import TraderId, StrategyId, InstrumentId
from nautilus_trader.model.objects import Price, Quantity


def _build_offline_exec_client() -> LighterExecutionClient:
    """Construct a LighterExecutionClient suitable for offline unit-style tests.

    This avoids network calls by:
      - Using dummy HTTP/WS URLs.
      - Never calling `_connect()` or any transport methods.
    """
    loop = None  # not used in these tests
    clock = LiveClock()
    trader = TraderId("TRADER-UNIT")
    strat = StrategyId("STRAT-UNIT")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    # Minimal credentials; signer is only constructed, not used to sign.
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

    # Instrument provider is not used in these tests; pass `None` for type ignore.
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


def _exercise_order_lifecycle() -> None:
    exec_client = _build_offline_exec_client()
    clock = exec_client._clock  # type: ignore[attr-defined]
    msgbus = exec_client._msgbus  # type: ignore[attr-defined]
    cache = exec_client._cache  # type: ignore[attr-defined]

    events: list[object] = []

    def on_event(evt: object) -> None:
        events.append(evt)

    msgbus.register(endpoint="ExecEngine.process", handler=on_event)

    # Prepare a dummy instrument + mapping from market_index -> instrument_id
    inst_id = InstrumentId.from_str("BNBUSDC-PERP.LIGHTER")
    market_index = 25
    exec_client._market_instrument[market_index] = inst_id  # type: ignore[attr-defined]

    # Create a limit order in cache for this instrument/strategy
    trader = msgbus.trader_id
    strat = StrategyId("STRAT-UNIT")
    of = OrderFactory(trader, strat, clock)
    order = of.limit(
        instrument_id=inst_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str("0.02"),
        price=Price.from_str("100.00"),
        time_in_force=TimeInForce.GTC,
        post_only=True,
    )
    cache.add_order(order, None)

    coid = order.client_order_id
    client_order_index = 123456
    order_index = 987654321

    # Register mappings as _submit_order would do
    exec_client._client_order_index_by_coid[coid.value] = client_order_index  # type: ignore[attr-defined]
    exec_client._coid_by_client_order_index[client_order_index] = coid  # type: ignore[attr-defined]

    # 1) First ACCEPTED/open frame -> OrderAccepted
    od1 = LighterAccountOrderStrict(
        order_index=order_index,
        market_index=market_index,
        status="open",
        is_ask=False,
        client_order_index=client_order_index,
        remaining_base_amount="0.02",
        price="100.00",
    )
    exec_client._process_account_orders([od1])  # type: ignore[attr-defined]

    # 2) Second ACCEPTED/open frame with new price -> OrderUpdated
    od2 = LighterAccountOrderStrict(
        order_index=order_index,
        market_index=market_index,
        status="open",
        is_ask=False,
        client_order_index=client_order_index,
        remaining_base_amount="0.02",
        price="101.00",
    )
    exec_client._process_account_orders([od2])  # type: ignore[attr-defined]

    # 3) CANCELED frame -> OrderCanceled
    od3 = LighterAccountOrderStrict(
        order_index=order_index,
        market_index=market_index,
        status="canceled",
        is_ask=False,
        client_order_index=client_order_index,
        remaining_base_amount="0.00",
        price="101.00",
    )
    exec_client._process_account_orders([od3])  # type: ignore[attr-defined]

    # Assertions (script-style)
    names = [type(e).__name__ for e in events]
    counts = Counter(names)
    print("[STATE_MACHINE] events:", counts)

    assert counts.get("OrderAccepted", 0) == 1, "expected exactly one OrderAccepted"
    assert counts.get("OrderUpdated", 0) == 1, "expected exactly one OrderUpdated"
    assert counts.get("OrderCanceled", 0) == 1, "expected exactly one OrderCanceled"

    # Ensure no unexpected duplicates
    assert counts.get("OrderRejected", 0) == 0, "did not expect OrderRejected in this scenario"
    assert counts.get("OrderModifyRejected", 0) == 0, "did not expect OrderModifyRejected in this scenario"

    print("[OK] LighterExecutionClient order state-machine behaves as expected for open→open(price change)→canceled")


def main() -> None:
    try:
        _exercise_order_lifecycle()
    except AssertionError as exc:
        print("[FAIL]", str(exc))
        sys.exit(1)
    except RuntimeError as exc:
        # If signer library is missing, treat as skip instead of hard failure.
        msg = str(exc)
        if "lighter-go signer library not available" in msg:
            print("[SKIP] signer library unavailable; execution state-machine test skipped")
            sys.exit(0)
        raise
    print("[DONE] test_execution_state_machine")


if __name__ == "__main__":
    main()
