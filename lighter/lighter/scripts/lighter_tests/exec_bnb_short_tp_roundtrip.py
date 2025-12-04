#!/usr/bin/env python3
from __future__ import annotations

"""
BNB short + take-profit roundtrip using ``LighterExecutionClient``.

Flow (mainnet, market_id=25):
  1) Place a small maker SHORT limit order (SELL 0.02 BNB, post_only) near top-of-book.
  2) Wait until it fills and a short position is opened.
  3) Place a small maker BUY limit order (post_only) slightly below entry price for
     a tiny profit (take-profit).
  4) Wait until the take-profit fills and the position returns to flat.

During the run:
  - Execution events (OrderSubmitted / OrderAccepted / OrderFilled / OrderUpdated /
    OrderCanceled) are printed for the BNB instrument.
  - Position and basic account updates are observed via the MessageBus.
  - Private WS and HTTP are recorded when the following env vars are set:
      LIGHTER_EXEC_RECORD=/tmp/nautilus_exec_ws_bnb_short_tp_roundtrip.jsonl
      LIGHTER_HTTP_RECORD=/tmp/nautilus_http_bnb_short_tp_roundtrip.jsonl

Environment (defaults target mainnet unless overridden)
------------------------------------------------------
  LIGHTER_HTTP_URL   = https://mainnet.zklighter.elliot.ai
  LIGHTER_WS_URL     = wss://mainnet.zklighter.elliot.ai/stream
  LIGHTER_PUBLIC_KEY = ...
  LIGHTER_PRIVATE_KEY= ...
  LIGHTER_ACCOUNT_INDEX = ...
  LIGHTER_API_KEY_INDEX = ...
"""

import asyncio
import os
from decimal import Decimal, ROUND_FLOOR, ROUND_UP
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
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.reports import (
    FillReport,
    OrderStatusReport,
    PositionStatusReport,
)
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import StrategyId, TraderId
from nautilus_trader.model.objects import Price, Quantity


async def _orderbook_imbalance(
    http: LighterPublicHttpClient,
    market_id: int,
    depth: int = 10,
) -> tuple[Decimal | None, Decimal, Decimal, Decimal | None, Decimal | None]:
    """
    Compute a simple order book imbalance metric for the given market.

    Returns
    -------
    (imbalance, bid_notional, ask_notional, best_bid, best_ask)

    imbalance = bid_notional / (bid_notional + ask_notional) in [0,1],
    or None if there is no depth on either side.
    """
    ob = await http.get_order_book_orders(market_id=market_id, limit=depth)
    bids = ob.get("bids") or []
    asks = ob.get("asks") or []

    def _side_notional(levels: list[dict]) -> Decimal:
        total = Decimal("0")
        for lvl in levels:
            try:
                px = Decimal(str(lvl.get("price")))
                sz = Decimal(str(lvl.get("size")))
            except Exception:
                continue
            total += px * sz
        return total

    bid_notional = _side_notional(bids)
    ask_notional = _side_notional(asks)
    total = bid_notional + ask_notional
    imbalance: Decimal | None = None
    if total > 0:
        imbalance = bid_notional / total

    best_bid = None
    best_ask = None
    if isinstance(bids, list) and bids:
        try:
            best_bid = Decimal(str(bids[0].get("price")))
        except Exception:
            best_bid = None
    if isinstance(asks, list) and asks:
        try:
            best_ask = Decimal(str(asks[0].get("price")))
        except Exception:
            best_ask = None

    return imbalance, bid_notional, ask_notional, best_bid, best_ask


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

    # Default record paths, can be overridden by caller.
    os.environ.setdefault("LIGHTER_EXEC_RECORD", "/tmp/nautilus_exec_ws_bnb_short_tp_roundtrip.jsonl")
    os.environ.setdefault("LIGHTER_HTTP_RECORD", "/tmp/nautilus_http_bnb_short_tp_roundtrip.jsonl")
     # Enable detailed adapter diagnostics for this test only (can be overridden by caller).
    os.environ.setdefault("LIGHTER_DEBUG", "1")
    os.environ.setdefault("LIGHTER_EXEC_LOG", "/tmp/lighter_exec_debug.log")

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("NAUT-BNB-SHORT-TP")
    strat = StrategyId("NAUT-BNB-SHORT-TP")
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
    mid = int(inst.info.get("market_id"))
    print(f"[SETUP] using instrument {inst_id}, market_id={mid}")

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

    # --- Event wiring -----------------------------------------------------------------
    entry_coid = None
    tp_coid = None
    entry_fill_price: Decimal | None = None
    tp_fill_price: Decimal | None = None
    last_position_size: Decimal | None = None

    entry_filled = asyncio.Event()
    tp_filled = asyncio.Event()

    def on_exec_event(evt: Any) -> None:
        nonlocal entry_fill_price, tp_fill_price
        name = type(evt).__name__
        # Focus on BNB instrument only
        inst_evt = getattr(evt, "instrument_id", None)
        if inst_evt is not None and inst_evt != inst_id:
            return

        if name in {"OrderSubmitted", "OrderAccepted", "OrderUpdated", "OrderCanceled", "OrderRejected", "OrderModifyRejected", "OrderFilled"}:
            coid = getattr(evt, "client_order_id", None)
            voi = getattr(evt, "venue_order_id", None)
            status = getattr(evt, "order_status", None)
            reason = getattr(evt, "reason", None)
            print(f"[EVENT] {name} coid={coid} voi={voi} status={status} reason={reason}")

        if name == "OrderFilled":
            coid = getattr(evt, "client_order_id", None)
            price_obj = getattr(evt, "price", None)
            try:
                px = price_obj.as_decimal() if hasattr(price_obj, "as_decimal") else Decimal(str(price_obj))
            except Exception:
                px = None
            if coid == entry_coid and not entry_filled.is_set():
                entry_fill_price = px
                entry_filled.set()
                print(f"[ENTRY-FILL] price={entry_fill_price}")
            elif coid == tp_coid and not tp_filled.is_set():
                tp_fill_price = px
                tp_filled.set()
                print(f"[TP-FILL] price={tp_fill_price}")

    def on_exec_report(report: Any) -> None:
        """Handle execution reports (fills, order status, positions).

        These are sent on the `ExecEngine.reconcile_execution_report` endpoint.
        """
        nonlocal last_position_size

        inst_rep = getattr(report, "instrument_id", None)
        if inst_rep is not None and inst_rep != inst_id:
            return

        if isinstance(report, FillReport):
            coid = getattr(report, "client_order_id", None)
            voi = getattr(report, "venue_order_id", None)
            last_qty = report.last_qty.as_decimal()
            last_px = report.last_px.as_decimal()
            print(f"[REPORT/FILL] coid={coid} voi={voi} qty={last_qty} px={last_px}")
            return

        if isinstance(report, OrderStatusReport):
            coid = getattr(report, "client_order_id", None)
            voi = getattr(report, "venue_order_id", None)
            status = getattr(report, "order_status", None)
            print(f"[REPORT/ORDER] coid={coid} voi={voi} status={status}")
            return

        if isinstance(report, PositionStatusReport):
            qty = report.quantity
            try:
                last_position_size = qty.as_decimal()
            except Exception:
                last_position_size = None
            side = getattr(report, "position_side", None)
            avg_px_open = getattr(report, "avg_px_open", None)
            print(
                f"[REPORT/POSITION] side={side} qty={last_position_size} "
                f"avg_px_open={avg_px_open}",
            )

    # Basic account monitor (optional)
    def on_account_state(evt: Any) -> None:
        name = type(evt).__name__
        if name != "AccountState":
            return
        balances = getattr(evt, "balances", []) or []
        if balances:
            first = balances[0]
            total = getattr(first, "total", None)
            free = getattr(first, "free", None)
            print(f"[ACCOUNT] total={total} free={free}")

    msgbus.register(endpoint="ExecEngine.process", handler=on_exec_event)
    msgbus.register(endpoint="ExecEngine.reconcile_execution_report", handler=on_exec_report)
    msgbus.register(endpoint="Portfolio.update_account", handler=on_account_state)

    # --- Connect ---------------------------------------------------------------------
    print("[CONNECT] connecting execution client…")
    await exec_client._connect()  # type: ignore[protected-access]
    if exec_client._ws is not None:
        await exec_client._ws.wait_until_ready(10)

    # --- ENTRY: maker LONG (BUY) 0.02 BNB -----------------------------------------
    # Use order book imbalance to pick a more buyer-dominant moment for the entry.
    print("[OB] scanning order book for buyer imbalance...")
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    target_imbalance = Decimal("0.65")  # require stronger buyer imbalance
    target_zscore = 1.0  # require imbalance to be meaningfully above its recent mean
    ob_timeout_secs = 60.0
    ob_poll_interval = 2.0
    deadline = loop.time() + ob_timeout_secs
    last_snapshot: tuple[Decimal | None, Decimal, Decimal, Decimal | None, Decimal | None] | None = None
    imb_history: list[Decimal] = []

    while loop.time() < deadline:
        try:
            imb, bid_notional, ask_notional, ob_bid, ob_ask = await _orderbook_imbalance(http_pub, mid)
            last_snapshot = (imb, bid_notional, ask_notional, ob_bid, ob_ask)
            zscore: float | None = None
            if imb is not None:
                imb_history.append(imb)
                # Compute simple z-score of current imbalance vs recent history
                if len(imb_history) >= 5:
                    nums = [float(x) for x in imb_history]
                    mean = sum(nums) / len(nums)
                    var = sum((x - mean) ** 2 for x in nums) / len(nums)
                    std = var ** 0.5
                    if std > 0:
                        zscore = (float(imb) - mean) / std
            z_str = f"{zscore:.3f}" if zscore is not None else "None"
            print(
                f"[OB] imbalance={imb} zscore={z_str} "
                f"bid_notional={bid_notional} ask_notional={ask_notional} "
                f"best_bid={ob_bid} best_ask={ob_ask}"
            )
            if (
                imb is not None
                and imb >= target_imbalance
                and zscore is not None
                and zscore >= target_zscore
                and ob_bid is not None
            ):
                best_bid, best_ask = ob_bid, ob_ask
                print(
                    "[OB] buyer imbalance and z-score sufficient; "
                    "proceeding with entry at bid side."
                )
                break
        except Exception as e:
            print(f"[OB] imbalance check failed: {e}")
        await asyncio.sleep(ob_poll_interval)

    if best_bid is None or best_ask is None:
        # Fallback: use last snapshot or a single fresh book if needed.
        if last_snapshot is not None:
            _, _, _, ob_bid, ob_ask = last_snapshot
            best_bid, best_ask = ob_bid, ob_ask
        if best_bid is None or best_ask is None:
            # Final fallback: one more snapshot
            try:
                _, _, _, ob_bid, ob_ask = await _orderbook_imbalance(http_pub, mid)
                best_bid = best_bid or ob_bid
                best_ask = best_ask or ob_ask
            except Exception:
                pass

    tick = Decimal(str(inst.price_increment))

    if best_bid is not None:
        # Maker BUY near top-of-book on bid side.
        entry_px = best_bid
    else:
        base_px = best_ask if best_ask is not None else None
        entry_px = base_px or Decimal("900")

    if tick > 0:
        steps = (entry_px / tick).to_integral_value(rounding=ROUND_FLOOR)
        entry_px = steps * tick

    qty_dec = Decimal("0.02")
    min_notional = Decimal(str(inst.min_notional.as_decimal()))
    notional = entry_px * qty_dec
    if notional < min_notional:
        # Scale quantity up to satisfy min_notional.
        target_qty = (min_notional / max(entry_px, Decimal("1e-9"))).quantize(
            Decimal(str(inst.size_increment)),
            rounding=ROUND_UP,
        )
        print(f"[WARN] notional {notional} < min_notional {min_notional}, bumping qty to {target_qty}")
        qty_dec = target_qty

    print(f"[ENTRY] long BUY maker: price={entry_px} qty={qty_dec} notional={entry_px * qty_dec}")

    of = OrderFactory(trader, strat, clock)
    entry_order = of.limit(
        instrument_id=inst_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str(str(qty_dec)),
        price=Price.from_str(str(entry_px)),
        time_in_force=TimeInForce.GTC,
        post_only=True,
    )
    cache.add_order(entry_order, None)
    entry_coid = entry_order.client_order_id

    submit_entry = SubmitOrder(
        trader_id=trader,
        strategy_id=strat,
        position_id=None,
        order=entry_order,
        command_id=UUID4(),
        ts_init=clock.timestamp_ns(),
    )
    exec_client.submit_order(submit_entry)

    # Wait a longer period for a likely fill; continue even if not confirmed.
    entry_wait_secs = 60.0
    await asyncio.sleep(entry_wait_secs)
    if not entry_filled.is_set():
        print(f"[WARN] entry long order not confirmed filled after {entry_wait_secs}s; "
              f"continuing with price={entry_px}")
        if entry_fill_price is None:
            entry_fill_price = entry_px
    if entry_fill_price is None:
        entry_fill_price = entry_px
    print(f"[ENTRY] using entry price={entry_fill_price}")

    # --- TP: maker SELL to close long with small profit ---------------------------
    # Reuse order book snapshot for TP pricing (no separate subscription needed).
    _, _, _, best_bid2, best_ask2 = await _orderbook_imbalance(http_pub, mid)
    tp_raw = entry_fill_price * Decimal("1.0005")  # ~0.05% profit on the long
    if best_ask2 is not None:
        # Stay on or above current best ask to remain maker on the ask side.
        tp_px = max(tp_raw, best_ask2)
    else:
        tp_px = tp_raw

    if tick > 0:
        steps = (tp_px / tick).to_integral_value(rounding=ROUND_FLOOR)
        tp_px = steps * tick

    print(f"[TP] SELL maker: price={tp_px} qty={qty_dec}")

    tp_order = of.limit(
        instrument_id=inst_id,
        order_side=OrderSide.SELL,
        quantity=Quantity.from_str(str(qty_dec)),
        price=Price.from_str(str(tp_px)),
        time_in_force=TimeInForce.GTC,
        post_only=True,
    )
    cache.add_order(tp_order, None)
    tp_coid = tp_order.client_order_id

    submit_tp = SubmitOrder(
        trader_id=trader,
        strategy_id=strat,
        position_id=None,
        order=tp_order,
        command_id=UUID4(),
        ts_init=clock.timestamp_ns(),
    )
    exec_client.submit_order(submit_tp)

    # Allow time for TP to potentially fill; do not block the entire run on it.
    tp_wait_secs = 90.0
    await asyncio.sleep(tp_wait_secs)
    if not tp_filled.is_set():
        print(f"[WARN] take-profit order not confirmed filled after {tp_wait_secs}s")
    else:
        print(f"[TP] filled at price={tp_fill_price}")

    # Allow some additional time for final position/account reports
    await asyncio.sleep(5.0)
    print(f"[SUMMARY] last observed BNB position size={last_position_size}")

    await exec_client._disconnect()  # type: ignore[protected-access]
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
