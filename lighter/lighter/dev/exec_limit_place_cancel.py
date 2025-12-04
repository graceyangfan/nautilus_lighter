from __future__ import annotations

"""
Place a LIMIT order (near top-of-book) via LighterExecutionClient on testnet, then cancel it,
logging sendtx_result and account_all (orders/trades/positions) to a log file for verification.

Usage:
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python -m nautilus_trader.adapters.lighter.dev.exec_limit_place_cancel \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --market-id 0 \
    --side buy \
    --ticks 1 \
    --notional 10 \
    --cancel-delay 120 \
    --runtime 600 \
    --log-file /tmp/lighter_exec_limit_place_cancel.log
"""

import argparse
import asyncio
import hashlib
import math
from typing import Any

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import SubmitOrder, CancelOrder, CancelAllOrders
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import TraderId, StrategyId, PositionId, VenueOrderId
from nautilus_trader.model.objects import Price, Quantity


async def _best_prices(http: LighterPublicHttpClient, mid: int) -> tuple[float | None, float | None]:
    try:
        doc = await http.get_order_book_orders(mid, limit=5)
        def first_px(arr):
            if not isinstance(arr, list) or not arr:
                return None
            x = arr[0].get("price")
            return float(x) if x is not None else None
        return first_px(doc.get("bids")), first_px(doc.get("asks"))
    except Exception:
        return None, None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secrets", required=True)
    ap.add_argument("--http", required=True)
    ap.add_argument("--ws", required=True)
    ap.add_argument("--market-id", type=int, default=0)
    ap.add_argument("--side", choices=["buy", "sell"], default="buy")
    ap.add_argument("--ticks", type=int, default=1)
    ap.add_argument("--notional", type=float, default=10.0)
    ap.add_argument("--cancel-delay", type=int, default=120)
    ap.add_argument("--runtime", type=int, default=600)
    ap.add_argument("--log-file", type=str, default="/tmp/lighter_exec_limit_place_cancel.log")
    ap.add_argument("--direct-await", action="store_true", help="Call internal async submit/cancel directly for debugging")
    args = ap.parse_args()

    # Simple file logger
    log_path = args.log_file
    def _log(*parts: Any) -> None:
        try:
            import time
            line = " ".join(str(p) for p in parts)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {line}\n")
        except Exception:
            pass

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("TRADER-LIGHTER-EXEC")
    strat = StrategyId("STRAT-TEST")
    pos_id = PositionId("POS-LIGHTER-TEST")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    _log("START limit_place_cancel", f"mid={args.market_id}", f"side={args.side}", f"ticks={args.ticks}", f"notional={args.notional}")

    # Provider
    http_pub = LighterPublicHttpClient(args.http)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    # Prefetch base symbol for the requested market to narrow provider load
    base_filter = None
    try:
        _det = await http_pub.get_order_book_details(int(args.market_id))
        if isinstance(_det, dict) and _det.get("symbol"):
            base_filter = str(_det["symbol"]).upper()
            _log("prefilter:", f"market_id={args.market_id}", f"base={base_filter}")
    except Exception:
        pass
    if base_filter:
        await provider.load_all_async(filters={"bases": [base_filter]})
    else:
        await provider.load_all_async()
    _log("provider_loaded:", f"count={len(provider.get_all())}")

    # Find instrument by market_id
    inst_id = None
    inst = None
    for iid, it in provider.get_all().items():
        try:
            if int(it.info.get("market_id")) == int(args.market_id):  # type: ignore[union-attr]
                inst_id = iid
                inst = it
                break
        except Exception:
            continue
    if inst_id is None or inst is None:
        _log("ERROR: no instrument matched market-id", args.market_id)
        return 2

    # Execution client
    creds = LighterCredentials.from_json_file(args.secrets)
    exec_cfg = LighterExecClientConfig(
        base_url_http=args.http,
        base_url_ws=args.ws,
        credentials=creds,
        chain_id=300,
        post_submit_http_reconcile=True,
        post_submit_poll_attempts=4,
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

    # Wrap callbacks
    orig_accall = exec_client._handle_account_all
    orig_sendtx = exec_client._handle_sendtx_result
    state: dict[str, Any] = {"our_coi": None, "our_oi": None}

    def _wrap_sendtx(obj: dict[str, Any]) -> None:
        try:
            data = obj.get("data") if isinstance(obj, dict) else None
            if isinstance(data, dict):
                _log("sendtx_result:", f"status={data.get('status')}", f"code={data.get('code')}", f"tx_hash={data.get('tx_hash')}", f"error={data.get('error')}")
            else:
                _log("sendtx_result: (unparsed)", str(obj)[:200])
        except Exception:
            pass
        orig_sendtx(obj)

    def _wrap_accall(msg) -> None:
        try:
            data = msg.data
            orders = data.orders or []
            trades = data.trades or []
            positions = data.positions or []
            _log(f"acc_all:+orders={len(orders)} +trades={len(trades)} +pos={len(positions)}")
            for od in orders:
                oi = od.order_index
                coi = od.client_order_index
                st = od.status
                px = od.price
                _log("order:", f"oi={oi}", f"coi={coi}", f"status={st}", f"price={px}")
                if state.get("our_coi") is not None and coi is not None and int(coi) == int(state["our_coi"]):
                    state["our_oi"] = oi
            for tr in trades:
                _log("trade:", f"tid={tr.trade_id}", f"oi={tr.order_index}", f"px={tr.price}", f"sz={tr.size}")
            for ps in positions:
                _log("position:", f"mid={ps.market_index}", f"size={ps.size}", f"aep={ps.avg_entry_price}")
        except Exception:
            pass
        orig_accall(msg)

    exec_client._handle_sendtx_result = _wrap_sendtx  # type: ignore[method-assign]
    exec_client._handle_account_all = _wrap_accall  # type: ignore[method-assign]

    # Connect
    await exec_client._connect()  # type: ignore[attr-defined]
    _log("connected")

    # Compute price/qty near top-of-book (non-marketable)
    best_bid, best_ask = await _best_prices(http_pub, int(args.market_id))
    price_dec = int(inst.price_precision)
    size_dec = int(inst.size_precision)
    inc_px = 10 ** (-price_dec)
    side = args.side.lower()
    ticks = max(0, int(args.ticks))
    if side == "buy":
        target_px = (best_bid or 0.0) + ticks * inc_px
        if best_ask is not None:
            target_px = min(target_px, best_ask - inc_px)
        order_side = OrderSide.BUY
    else:
        target_px = (best_ask or 0.0) - ticks * inc_px
        if best_bid is not None:
            target_px = max(target_px, best_bid + inc_px)
        order_side = OrderSide.SELL

    # Qty from notional (>= min_quantity), align to actual increment
    step_sz = float(str(inst.size_increment)) if float(str(inst.size_increment)) > 0 else 10 ** (-size_dec)
    qty_est = max(float(inst.min_quantity), float(args.notional) / max(target_px, 1e-9))
    qty = math.ceil(qty_est / step_sz) * step_sz
    _log("computed:", f"best_bid={best_bid}", f"best_ask={best_ask}", f"px={target_px}", f"qty={qty}")

    # Build LIMIT GTC (post-only 由 venue tif 规则映射，这里使用 GTC/GTT 均可；为避免成交，用接近盘口且不跨价)
    of = OrderFactory(trader, strat, clock)
    order = of.limit(
        instrument_id=inst_id,
        order_side=order_side,
        quantity=Quantity.from_str(f"{qty:.{size_dec}f}"),
        price=Price.from_str(f"{target_px:.{price_dec}f}"),
        time_in_force=TimeInForce.GTC,
        reduce_only=False,
    )
    # Compute our client_order_index digest (same scheme as execution _prepare_order)
    coid_value = order.client_order_id.value
    digest = hashlib.blake2b(coid_value.encode("utf-8"), digest_size=8).digest()
    our_coi = int.from_bytes(digest, byteorder="big", signed=False)
    state["our_coi"] = int(our_coi)
    _log("open: submitting LIMIT", f"coi={our_coi}")

    cmd_open = SubmitOrder(
        trader_id=trader,
        strategy_id=strat,
        position_id=pos_id,
        order=order,
        command_id=UUID4(),
        ts_init=clock.timestamp_ns(),
    )
    if args.direct_await:
        await exec_client._submit_order(cmd_open)  # type: ignore[attr-defined]
    else:
        exec_client.submit_order(cmd_open)

    # Wait for the order to appear in account_all (up to cancel_delay)
    deadline = clock.timestamp_ns() + max(5, int(args.cancel_delay)) * 1_000_000_000
    while clock.timestamp_ns() < deadline and state.get("our_oi") is None:
        await asyncio.sleep(1.0)

    # If we have an order_index, send CancelOrder; else fallback to CancelAll (side-specific)
    if state.get("our_oi") is not None:
        oi = int(state["our_oi"])  # venue order index
        cmd_cancel = CancelOrder(
            trader_id=trader,
            strategy_id=strat,
            instrument_id=inst_id,
            client_order_id=None,
            venue_order_id=VenueOrderId(str(oi)),
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        )
        _log("cancel: submitting for oi=", oi)
        if args.direct_await:
            await exec_client._cancel_order(cmd_cancel)  # type: ignore[attr-defined]
        else:
            exec_client.cancel_order(cmd_cancel)
    else:
        _log("WARN: our order_index not observed; submitting CancelAll fallback")
        cmd_cancel_all = CancelAllOrders(
            trader_id=trader,
            strategy_id=strat,
            instrument_id=inst_id,
            order_side=order_side,
            command_id=UUID4(),
            ts_init=clock.timestamp_ns(),
        )
        if args.direct_await:
            await exec_client._cancel_all_orders(cmd_cancel_all)  # type: ignore[attr-defined]
        else:
            exec_client.cancel_all_orders(cmd_cancel_all)

    # Observe remaining window
    await asyncio.sleep(max(5, int(args.runtime)))
    await exec_client._disconnect()  # type: ignore[attr-defined]
    _log("summary: finished")
    print("Log written to:", log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
