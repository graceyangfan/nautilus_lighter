#!/usr/bin/env python3
from __future__ import annotations

"""
ExecutionClient market-fill smoke to validate trade/position/account callbacks.

This uses LighterExecutionClient to submit a MARKET IOC order via the command
path, observes account_all* callbacks (strict msgspec), and prints concise
results (fills, orders, positions, account snapshots).

Usage (run outside repo root to avoid import shadowing):

  source ~/nautilus_trader-develop/.venv/bin/activate
  cd /tmp
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python /root/nautilus_trader-develop/scripts/lighter_tests/exec_client_market_fill.py \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --base ETH \
    --notional 8 \
    --runtime 45
"""

import argparse
import asyncio
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.nautilus_pyo3 import Quota
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import TraderId, StrategyId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.common.factories import OrderFactory


async def _best_prices(http: LighterPublicHttpClient, mid: int) -> tuple[Decimal | None, Decimal | None, int, int]:
    det = await http.get_order_book_details(mid)
    price_decimals = int(det.get("price_decimals") or det.get("supported_price_decimals") or 0) if isinstance(det, dict) else 0
    size_decimals = int(det.get("size_decimals") or 0) if isinstance(det, dict) else 0
    ob = await http.get_order_book_orders(market_id=mid, limit=5)
    def first_px(arr: list[dict] | None) -> Decimal | None:
        if not isinstance(arr, list) or not arr:
            return None
        x = arr[0].get("price")
        return Decimal(str(x)) if x is not None else None
    bid = first_px(ob.get("bids"))
    ask = first_px(ob.get("asks"))
    return bid, ask, price_decimals, size_decimals


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", default="https://testnet.zklighter.elliot.ai")
    ap.add_argument("--ws", default="wss://testnet.zklighter.elliot.ai/stream")
    ap.add_argument("--secrets", default="secrets/lighter_testnet_account.json")
    ap.add_argument("--base", default="ETH")
    ap.add_argument("--notional", type=float, default=8.0)
    ap.add_argument("--runtime", type=int, default=45)
    args = ap.parse_args()

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("TRADER-LIGHTER-EXEC")
    strat = StrategyId("STRAT-MKT")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    quotas = [
        ("lighter:/api/v1/orderBooks", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookDetails", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookOrders", Quota.rate_per_minute(12)),
    ]
    http_pub = LighterPublicHttpClient(base_url=args.http, ratelimiter_quotas=quotas)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async(filters={"bases": [args.base.strip().upper()]})
    if not provider.get_all():
        print("No instruments loaded; aborting")
        return 1
    inst_id, inst = next(iter(provider.get_all().items()))
    mid = int(inst.info["market_id"])
    print("Using instrument:", inst_id, "market_id=", mid)

    creds = LighterCredentials.from_json_file(args.secrets)
    exec_cfg = LighterExecClientConfig(
        base_url_http=args.http,
        base_url_ws=args.ws,
        credentials=creds,
        chain_id=300,
        subscribe_account_stats=False,
        subscribe_split_account_channels=True,
        use_python_ws_private=True,
        post_submit_http_reconcile=True,
        post_submit_poll_attempts=3,
        post_submit_poll_interval_ms=1500,
    )
    exec_client = LighterExecutionClient(
        loop=loop,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,
        base_url_http=args.http,
        base_url_ws=args.ws,
        credentials=creds,
        config=exec_cfg,
    )

    # Observe ExecEngine + Portfolio updates
    def on_exec_event(evt: Any) -> None:
        try:
            name = type(evt).__name__
            coid = getattr(evt, "client_order_id", None)
            voi = getattr(evt, "venue_order_id", None)
            print("[Exec]", name, coid, voi)
        except Exception:
            pass
    def on_account_state(evt: Any) -> None:
        try:
            bals = list(getattr(evt, "balances", []) or [])
            if bals:
                print("[Account] total=", getattr(bals[0], "total", None), "free=", getattr(bals[0], "free", None))
        except Exception:
            pass
    msgbus.register(endpoint="ExecEngine.process", handler=on_exec_event)
    msgbus.register(endpoint="Portfolio.update_account", handler=on_account_state)

    # Count strict private updates
    counters = {"orders": 0, "trades": 0, "positions": 0}
    orig_o = exec_client._process_account_orders
    orig_t = exec_client._process_account_trades
    orig_p = exec_client._process_account_positions
    def wrap_o(ods): counters["orders"] += len(ods or []); return orig_o(ods)
    def wrap_t(ts): counters["trades"] += len(ts or []); return orig_t(ts)
    def wrap_p(ps): counters["positions"] += len(ps or []); return orig_p(ps)
    exec_client._process_account_orders = wrap_o  # type: ignore[method-assign]
    exec_client._process_account_trades = wrap_t  # type: ignore[method-assign]
    exec_client._process_account_positions = wrap_p  # type: ignore[method-assign]

    print("[connect] …")
    await exec_client._connect()
    if exec_client._ws is not None:
        await exec_client._ws.wait_until_ready(10)

    # Derive a small base amount for MARKET IOC
    bid, ask, pdec, sdec = await _best_prices(http_pub, mid)
    px = ask or bid or Decimal("2000")
    step = Decimal(10) ** Decimal(-(sdec or 0))
    nominal = Decimal(str(args.notional)) / (px if px > 0 else Decimal("2000"))
    k = (nominal / step).to_integral_value(rounding="ROUND_UP")
    base = max(step, k * step)

    of = OrderFactory(trader, strat, clock)
    order = of.market(
        instrument_id=inst_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str(str(base)),
        time_in_force=TimeInForce.IOC,
    )
    print("Submitting MARKET IOC:", order)
    submit = SubmitOrder(trader, strat, None, order, UUID4(), clock.timestamp_ns())
    exec_client.submit_order(submit)

    # Observe for runtime seconds
    await asyncio.sleep(max(10, args.runtime))

    await exec_client._disconnect()
    print("Counters:", counters)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
