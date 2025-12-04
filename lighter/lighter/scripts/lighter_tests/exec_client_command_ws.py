#!/usr/bin/env python3
from __future__ import annotations

"""
LighterExecutionClient command-based WS smoke: SubmitOrder via WS + CancelAllOrders via WS,
and verify that account value is locked while the order is open.

Usage:
  source ~/nautilus_trader-develop/.venv/bin/activate
  cd /tmp
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python /root/nautilus_trader-develop/scripts/lighter_tests/exec_client_command_ws.py \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --base ETH \
    --runtime 60
"""

import argparse
import os
import asyncio
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.schemas.ws import LighterAccountStatsStrict
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.nautilus_pyo3 import Quota
from nautilus_trader.execution.messages import SubmitOrder, CancelAllOrders
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import TraderId, StrategyId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.core.uuid import UUID4


async def _best_prices(http: LighterPublicHttpClient, mid: int) -> tuple[Decimal | None, Decimal | None]:
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
    ap.add_argument("--http", default="https://mainnet.zklighter.elliot.ai")
    ap.add_argument("--ws", default="wss://mainnet.zklighter.elliot.ai/stream")
    ap.add_argument("--secrets", default="secrets/lighter_testnet_account.json")
    ap.add_argument("--base", default="BNB")
    ap.add_argument("--runtime", type=int, default=60)
    ap.add_argument("--chain-id", type=int, default=304, help="chain id (300=testnet, 304=mainnet)")
    ap.add_argument("--post-only", action="store_true", default=False, help="submit limit as post-only (maker)")
    args = ap.parse_args()

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("TRADER-LIGHTER-EXEC")
    strat = StrategyId("STRAT-TEST")
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

    # Load credentials from environment variables
    pubkey = os.getenv("LIGHTER_PUBLIC_KEY")
    private_key = os.getenv("LIGHTER_PRIVATE_KEY")
    account_index = os.getenv("LIGHTER_ACCOUNT_INDEX")
    api_key_index = os.getenv("LIGHTER_API_KEY_INDEX", "0")

    if not all([pubkey, private_key, account_index]):
        print("ERROR: Missing environment variables")
        print("Required: LIGHTER_PUBLIC_KEY, LIGHTER_PRIVATE_KEY, LIGHTER_ACCOUNT_INDEX")
        return 1

    creds = LighterCredentials(
        pubkey=pubkey,
        account_index=int(account_index),
        api_key_index=int(api_key_index),
        private_key=private_key,
    )
    exec_cfg = LighterExecClientConfig(
        base_url_http=args.http,
        base_url_ws=args.ws,
        credentials=creds,
        chain_id=int(args.chain_id),
        subscribe_account_stats=False,  # 主网不可用
        post_submit_http_reconcile=True,
        post_submit_poll_attempts=3,
        post_submit_poll_interval_ms=1500,
        use_python_ws_private=True,
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

    # Subscribe MessageBus endpoints to print events
    def on_exec_event(evt: Any) -> None:
        try:
            name = type(evt).__name__
            extras = {}
            # Best-effort extraction of common fields
            if hasattr(evt, "client_order_id"):
                extras["client_order_id"] = getattr(evt, "client_order_id", None)
            if hasattr(evt, "venue_order_id"):
                extras["venue_order_id"] = getattr(evt, "venue_order_id", None)
            if hasattr(evt, "instrument_id"):
                extras["instrument_id"] = getattr(evt, "instrument_id", None)
            print("[MsgBus][ExecEngine.process]", name, extras)
        except Exception:
            pass

    def on_account_state(evt: Any) -> None:
        try:
            name = type(evt).__name__
            # evt has balances list; print total/available if present
            total = None; free = None
            try:
                bals = list(getattr(evt, "balances", []) or [])
                if bals:
                    total = getattr(bals[0], "total", None)
                    free = getattr(bals[0], "free", None)
            except Exception:
                pass
            print("[MsgBus][Portfolio.update_account]", name, "total=", total, "free=", free)
        except Exception:
            pass

    msgbus.register(endpoint="ExecEngine.process", handler=on_exec_event)
    msgbus.register(endpoint="Portfolio.update_account", handler=on_account_state)

    # Track account_stats to observe locked balance changes (WS stream)
    stats_log: list[tuple[float, float]] = []  # (total, available)
    _orig_handle_stats = exec_client._handle_account_stats
    _orig_handle_sendtx = exec_client._handle_sendtx_result
    _orig_handle_accall = exec_client._handle_account_all

    def _wrap_stats(s: LighterAccountStatsStrict) -> None:
        try:
            stats_log.append((float(s.total_balance), float(s.available_balance)))
            print(f"[account_stats] total={s.total_balance} available={s.available_balance}")
        except Exception:
            pass
        _orig_handle_stats(s)

    exec_client._handle_account_stats = _wrap_stats  # type: ignore[method-assign]
    
    def _wrap_sendtx(obj: Any) -> None:
        try:
            data = getattr(obj, 'data', None)
            code = getattr(obj, 'code', None)
            if data is not None:
                status = getattr(data, 'status', None)
                tx_hash = getattr(data, 'tx_hash', None)
                order_index = getattr(data, 'order_index', None)
                coi = getattr(data, 'client_order_index', None)
                print(f"[sendtx_result] code={code} status={status} tx_hash={tx_hash} oi={order_index} coi={coi}")
        except Exception:
            pass
        _orig_handle_sendtx(obj)

    def _wrap_accall(msg: Any) -> None:
        try:
            orders = getattr(getattr(msg, 'orders', None), '__len__', lambda: 0)()
            trades = getattr(getattr(msg, 'trades', None), '__len__', lambda: 0)()
            positions = getattr(getattr(msg, 'positions', None), '__len__', lambda: 0)()
            print(f"[account_all*] orders={orders} trades={trades} positions={positions}")
        except Exception:
            pass
        _orig_handle_accall(msg)

    exec_client._handle_sendtx_result = _wrap_sendtx  # type: ignore[method-assign]
    exec_client._handle_account_all = _wrap_accall    # type: ignore[method-assign]

    print("[exec] connecting...")
    await exec_client._connect()  # ensure provider/map ready
    print("[exec] connected")
    if exec_client._ws is not None:
        await exec_client._ws.wait_until_ready(10)

    # Snapshot initial locked via HTTP
    state0 = await exec_client._http_account.get_account_state_strict(account_index=creds.account_index, auth=exec_client._signer.create_auth_token())
    total0 = Decimal(str(state0.total_balance))
    avail0 = Decimal(str(state0.available_balance))
    locked0 = (total0 - avail0) if (total0 >= avail0) else Decimal("0")
    print(f"[HTTP] before total={total0} available={avail0} locked={locked0}")

    # Prepare a non-marketable buy order near top
    best_bid, best_ask = await _best_prices(http_pub, mid)
    if best_bid is None and best_ask is None:
        best_bid, best_ask = Decimal("2000"), Decimal("2010")
    tick = Decimal(str(inst.price_increment))
    px = best_bid if best_bid is not None else (best_ask or Decimal("2000"))
    if best_ask is not None:
        px = min(px + tick, best_ask - tick)
    # Fixed quantity: 0.02 BNB
    qty_dec = Decimal("0.02")
    # Verify meets min notional
    min_notional = Decimal(str(inst.min_notional.as_decimal()))
    notional = px * qty_dec
    print(f"Order: {qty_dec} {args.base} @ {px} = {notional} USDC (min: {min_notional})")
    if notional < min_notional:
        print(f"ERROR: Notional {notional} < min {min_notional}")
        return 1

    of = OrderFactory(trader, strat, clock)
    order = of.limit(
        instrument_id=inst_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str(str(qty_dec)),
        price=Price.from_str(str(px.quantize(tick)) if tick > 0 else str(px)),
        time_in_force=TimeInForce.GTC,
        post_only=True,  # Always POST_ONLY on mainnet to avoid accidental fills
    )

    print("Submitting (WS) via SubmitOrder command")
    # Add order to cache with strategy_id before submitting
    cache.add_order(order, None)

    submit_cmd = SubmitOrder(
        trader_id=trader,
        strategy_id=strat,
        position_id=None,
        order=order,
        command_id=UUID4(),
        ts_init=clock.timestamp_ns(),
    )
    exec_client.submit_order(submit_cmd)

    # Wait and probe for locked increase
    increased = False
    deadline = clock.timestamp_ns() + max(10, args.runtime // 2) * 1_000_000_000
    while clock.timestamp_ns() < deadline:
        await asyncio.sleep(2.0)
        # Prefer stats stream; else sample HTTP
        if stats_log:
            total, avail = stats_log[-1]
            if total >= avail and (Decimal(str(total)) - Decimal(str(avail))) > locked0:
                increased = True
                break
        try:
            st = await exec_client._http_account.get_account_state_strict(account_index=creds.account_index, auth=exec_client._signer.create_auth_token())
            tot = Decimal(str(st.total_balance)); av = Decimal(str(st.available_balance))
            if tot >= av and (tot - av) > locked0:
                increased = True
                break
        except Exception:
            pass

    print("Locked increased:", increased)

    print("Sending CancelAllOrders (WS)")
    from nautilus_trader.execution.messages import CancelAllOrders
    cancel_all_cmd = CancelAllOrders(
        trader_id=trader,
        strategy_id=strat,
        instrument_id=inst_id,
        order_side=OrderSide.NO_ORDER_SIDE,
        command_id=UUID4(),
        ts_init=clock.timestamp_ns(),
    )
    exec_client.cancel_all_orders(cancel_all_cmd)

    await asyncio.sleep(max(5, args.runtime // 2))

    # Final snapshot
    st1 = await exec_client._http_account.get_account_state_strict(account_index=creds.account_index, auth=exec_client._signer.create_auth_token())
    tot1 = Decimal(str(st1.total_balance)); av1 = Decimal(str(st1.available_balance))
    lock1 = (tot1 - av1) if tot1 >= av1 else Decimal("0")
    print(f"[HTTP] after total={tot1} available={av1} locked={lock1}")

    await exec_client._disconnect()
    print("DONE (WS commands). Increased locked=", increased)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
