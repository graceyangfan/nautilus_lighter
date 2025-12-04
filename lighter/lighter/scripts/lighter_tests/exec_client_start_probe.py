#!/usr/bin/env python3
from __future__ import annotations

"""
Probe that ExecutionClient emits account/position/order callbacks on first start,
then place a resting LIMIT order to observe order callbacks, and optionally cancel.

Usage:
  source ~/nautilus_trader-develop/.venv/bin/activate
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python scripts/lighter_tests/exec_client_start_probe.py \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --secrets secrets/lighter_testnet_account.json \
    --base BTC \
    --runtime 90

Notes:
  - Prints concise markers when baseline HTTP emits AccountState/Positions and when
    private WS emits Orders/Positions/Trades.
  - Places a non-marketable LIMIT order so that an ACCEPTED order callback is likely.
  - Leaves the order resting (no cancel by default). Pass --cancel to send cancel-all.
"""

import argparse
import asyncio
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner
from nautilus_trader.adapters.lighter.common.enums import LighterOrderType, LighterTimeInForce, LighterCancelAllTif
from nautilus_trader.adapters.lighter.common.normalize import decimal_to_scaled_int
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.nautilus_pyo3 import Quota
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import TraderId, StrategyId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.common.factories import OrderFactory


async def best_prices(http: LighterPublicHttpClient, mid: int) -> tuple[Decimal | None, Decimal | None]:
    try:
        doc = await http.get_order_book_orders(mid, limit=5)
        def first_px(arr: list[dict] | None) -> Decimal | None:
            if not isinstance(arr, list) or not arr:
                return None
            x = arr[0].get("price")
            if x is None:
                return None
            return Decimal(str(x))
        return first_px(doc.get("bids")), first_px(doc.get("asks"))
    except Exception:
        return None, None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", required=True)
    ap.add_argument("--ws", required=True)
    ap.add_argument("--secrets", required=True)
    ap.add_argument("--base", default="BTC")
    ap.add_argument("--runtime", type=int, default=90)
    ap.add_argument("--cancel", action="store_true", help="send cancel-all after placing order")
    args = ap.parse_args()

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("TRADER-LIGHTER-PROBE")
    strat = StrategyId("STRAT-PROBE")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    quotas = [
        ("lighter:/api/v1/orderBooks", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookDetails", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookOrders", Quota.rate_per_minute(12)),
    ]
    http_pub = LighterPublicHttpClient(args.http, ratelimiter_quotas=quotas)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async(filters={"bases": [args.base.strip().upper()]})
    if not provider.get_all():
        print("[PROBE] no instruments loaded for base", args.base)
        return 1
    inst_id, inst = next(iter(provider.get_all().items()))
    mid = int(inst.info["market_id"])  # type: ignore[index]
    print("[PROBE] instrument:", inst_id, "market_id=", mid)

    creds = LighterCredentials.from_json_file(args.secrets)
    exec_cfg = LighterExecClientConfig(
        base_url_http=args.http,
        base_url_ws=args.ws,
        credentials=creds,
        chain_id=300 if "testnet" in args.http else 304,
        subscribe_account_stats=True,
        subscribe_split_account_channels=True,
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

    # Monkey-patch key emitters to print concise markers
    orig_acc = exec_client.generate_account_state
    def wrap_acc(*a, **kw):
        print("[EMIT][AccountState] reported=", kw.get("reported", True))
        return orig_acc(*a, **kw)
    exec_client.generate_account_state = wrap_acc  # type: ignore[assignment]

    orig_pos = exec_client._process_account_positions
    def wrap_pos(positions):
        print("[EMIT][Positions] count=", len(positions or []))
        return orig_pos(positions)
    exec_client._process_account_positions = wrap_pos  # type: ignore[assignment]

    orig_od = exec_client._process_account_orders
    def wrap_od(orders):
        print("[EMIT][Orders] count=", len(orders or []))
        return orig_od(orders)
    exec_client._process_account_orders = wrap_od  # type: ignore[assignment]

    # Connect (this triggers HTTP baseline emit + later WS subscriptions)
    print("[PROBE] connecting exec client…")
    await exec_client._connect()  # type: ignore[attr-defined]
    print("[PROBE] connected")

    # Prepare a non-marketable LIMIT order to rest on the book
    bid, ask = await best_prices(http_pub, mid)
    tick = Decimal(str(inst.price_increment))
    if bid is None and ask is None:
        bid, ask = Decimal("2000"), Decimal("2010")
    px = bid if bid is not None else (ask or Decimal("2000"))
    if ask is not None:
        px = min(px + tick, ask - tick)  # stay inside spread
    qty = Decimal(str(inst.min_quantity))
    min_notional = Decimal(str(inst.min_notional.as_decimal()))
    need = (min_notional / (px if px > 0 else Decimal("1")))
    step = Decimal(str(inst.size_increment))
    k = (need / step).to_integral_value(rounding="ROUND_UP")
    qty = max(qty, k * step)

    of = OrderFactory(trader, strat, clock)
    order = of.limit(
        instrument_id=inst_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str(str(qty)),
        price=Price.from_str(str(px.quantize(tick)) if tick > 0 else str(px)),
        time_in_force=TimeInForce.GTC,
    )
    print("[PROBE] submitting resting LIMIT …")
    submit_cmd = __import__("nautilus_trader.execution.messages", fromlist=["SubmitOrder"]).SubmitOrder(
        trader_id=trader, strategy_id=strat, position_id=None, order=order,
        command_id=__import__("nautilus_trader.core.uuid", fromlist=["UUID4"]).UUID4(), ts_init=clock.timestamp_ns(),
    )
    exec_client.submit_order(submit_cmd)

    # Optional: cancel-all after half runtime to observe cancel updates
    async def maybe_cancel_all():
        if not args.cancel:
            return
        await asyncio.sleep(max(10, args.runtime // 2))
        signer = LighterSigner(creds, base_url=args.http, chain_id=300 if "testnet" in args.http else 304)
        from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
        from nautilus_trader.adapters.lighter.http.transaction import LighterTransactionHttpClient
        acc = LighterAccountHttpClient(args.http)
        nonce = await acc.get_next_nonce(creds.account_index, creds.api_key_index)
        tx_info, err = await signer.sign_cancel_all_orders_tx(LighterCancelAllTif.IMMEDIATE, 0, int(nonce))
        if err:
            print("[PROBE][cancel-all] sign error:", err)
            return
        tok = signer.create_auth_token(deadline_seconds=600)
        tx = LighterTransactionHttpClient(args.http)
        res = await tx.send_tx(tx_type=signer.get_tx_type_cancel_all(), tx_info=tx_info, auth=tok)
        print("[PROBE][cancel-all] resp:", res)

    asyncio.create_task(maybe_cancel_all())

    await asyncio.sleep(args.runtime)
    await exec_client._disconnect()
    print("[PROBE] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

