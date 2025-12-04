#!/usr/bin/env python3
from __future__ import annotations

"""
Simple live TradingNode example for Lighter: single limit order place + cancel.

What this script does
---------------------
- Builds a `TradingNode` with:
  - `LighterDataClient` (public market data).
  - `LighterExecutionClient` (private account / orders, with reconciliation enabled).
  - A simple `LighterSimplePlaceCancelStrategy`:
      * Subscribes to L2 order book for BNBUSDC-PERP.LIGHTER.
      * On first usable book, submits a maker limit order away from the market
        (to avoid fills).
      * After a configurable delay, sends a CancelOrder for that client order ID.
      * Logs key order lifecycle events (Submitted / Accepted / Canceled / Rejected).

This is meant to verify end-to-end that:
- Strategy → DataEngine → LighterDataClient can receive order book data.
- Strategy → ExecEngine → LighterExecutionClient can submit and cancel orders.
- Execution reconciliation is running under `LiveExecutionEngine`.

Requirements
------------
Environment variables for your Lighter mainnet API key:

  export LIGHTER_PUBLIC_KEY="..."
  export LIGHTER_PRIVATE_KEY="..."
  export LIGHTER_ACCOUNT_INDEX=XXXXX
  export LIGHTER_API_KEY_INDEX=2

Optional:

  export LIGHTER_HTTP_URL="https://mainnet.zklighter.elliot.ai"
  export LIGHTER_WS_URL="wss://mainnet.zklighter.elliot.ai/stream"
  export LIGHTER_INSTRUMENT_ID="BNBUSDC-PERP.LIGHTER"
  export LIGHTER_LIVE_DEMO_SECS=60

Run example
-----------

  PYTHONPATH=/root/myenv/lib/python3.12/site-packages \\
  python adapters/lighter/scripts/lighter_tests/live_lighter_simple_place_cancel.py
"""

import os
from decimal import Decimal, ROUND_FLOOR, ROUND_UP

from nautilus_trader.adapters.lighter.common.constants import LIGHTER_VENUE
from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import (
    LighterDataClientConfig,
    LighterExecClientConfig,
)
from nautilus_trader.adapters.lighter.factories import (
    LighterLiveDataClientFactory,
    LighterLiveExecClientFactory,
)
from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.common.enums import LogColor
from nautilus_trader.config import StrategyConfig
from nautilus_trader.live.config import (
    LiveDataEngineConfig,
    LiveExecEngineConfig,
    LiveRiskEngineConfig,
    RoutingConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import OrderBookDeltas, CustomData, DataType
from nautilus_trader.model.enums import BookType, OrderSide, TimeInForce
from nautilus_trader.model.events.order import (
    OrderAccepted,
    OrderCanceled,
    OrderModifyRejected,
    OrderPendingUpdate,
    OrderRejected,
    OrderSubmitted,
    OrderUpdated,
)
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.data import TradeTick
from nautilus_trader.trading.config import ImportableStrategyConfig
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.adapters.lighter.common.types import LighterMarketStats


class LighterSimplePlaceCancelConfig(StrategyConfig, frozen=True):
    """
    Configuration for ``LighterSimplePlaceCancelStrategy``.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument to trade (e.g. BNBUSDC-PERP.LIGHTER).
    entry_qty : Decimal, default 0.02
        Target entry quantity.
    side : str, default \"BUY\"
        Side for the test order (\"BUY\" or \"SELL\").
    price_offset_pct : float, default 0.01
        Distance from best bid/ask when placing the order (1% away to avoid fills).
    modify_delay_secs : float, default 5.0
        Seconds to wait after OrderAccepted before sending a modify request.
    modify_size_factor : Decimal, default 1.5
        Factor to multiply the original quantity by when modifying the order.
    modify_mode : str, default "qty"
        Which field to modify: "qty" (size only), "price" (price only), or "both".
    wait_before_cancel_secs : float, default 20.0
        Seconds to wait before sending a cancel request.
    dry_run : bool, default False
        If True, no orders will actually be submitted/canceled.
    """

    instrument_id: InstrumentId
    entry_qty: Decimal = Decimal("0.02")
    side: str = "BUY"
    price_offset_pct: float = 0.01
    wait_before_cancel_secs: float = 20.0
    modify_delay_secs: float = 5.0
    modify_size_factor: Decimal = Decimal("1.5")
    modify_mode: str = "qty"
    dry_run: bool = False


class LighterSimplePlaceCancelStrategy(Strategy):
    """
    Very simple strategy:

    - Subscribes to L2 order book deltas.
    - On first usable book:
        * Computes a limit price away from the market (maker, unlikely to fill).
        * Submits a single limit order.
    - After `wait_before_cancel_secs` has elapsed, sends `CancelOrder` for the
      submitted order.
    - Logs key order events for verification.
    """

    def __init__(self, config: LighterSimplePlaceCancelConfig) -> None:
        super().__init__(config)
        self.instrument: Instrument | None = None
        self.book_type: BookType = BookType.L2_MBP
        self._submitted_coid = None
        self._submitted_ts_ns: int | None = None
        self._accepted_ts_ns: int | None = None
        self._cancel_sent: bool = False
        self._modified_sent: bool = False
        self._last_trade_px: Price | None = None
        self._data_log_count: int = 0  # debug counter for on_data

    def on_start(self) -> None:
        # Request instrument so it is available in the cache, then subscribe
        # to order book deltas for linkage with LighterDataClient.
        self.request_instrument(self.config.instrument_id)
        self.subscribe_order_book_deltas(self.config.instrument_id, self.book_type)
        # Subscribe to LighterMarketStats as CustomData so on_data receives market stats.
        self.subscribe_data(
            DataType(LighterMarketStats),
            instrument_id=self.config.instrument_id,
        )
        # Also subscribe trade ticks to verify TradeTick flow.
        self.subscribe_trade_ticks(self.config.instrument_id)
        self.log.info(
            f"[SIMPLE] Started for {self.config.instrument_id} side={self.config.side}",
            LogColor.BLUE,
        )

    def _ensure_instrument(self) -> bool:
        if self.instrument is None:
            self.instrument = self.cache.instrument(self.config.instrument_id)
            if self.instrument is None:
                self.log.warning(
                    f"[SIMPLE] instrument {self.config.instrument_id} not yet in cache",
                )
                return False
        return True

    # --- Order book handlers ---------------------------------------------------------------

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        if deltas.instrument_id != self.config.instrument_id:
            return
        # Log a compact view of top-of-book to verify OrderBook flow.
        book = self.cache.order_book(self.config.instrument_id)
        if book and book.spread():
            best_bid = book.best_bid_price()
            best_ask = book.best_ask_price()
            # Also log depth sizes to confirm book maintenance.
            try:
                bids = book.bids()
                asks = book.asks()
                nbids = len(bids)
                nasks = len(asks)
            except Exception:
                nbids = -1
                nasks = -1
            self.log.info(
                f"[SIMPLE] Book best_bid={best_bid} best_ask={best_ask} nbids={nbids} nasks={nasks}",
                LogColor.BLUE,
            )
        else:
            self.log.info(
                "[SIMPLE] Book not yet ready (no spread); waiting for more deltas",
                LogColor.BLUE,
            )
        self._maybe_submit_and_cancel()

    def on_order_book(self, order_book: OrderBook) -> None:
        if order_book.instrument_id != self.config.instrument_id:
            return
        self._maybe_submit_and_cancel()

    def _maybe_submit_and_cancel(self) -> None:
        now_ns = self.clock.timestamp_ns()

        # Submit once
        if self._submitted_coid is None:
            self._place_test_order(now_ns)
            return

        # After ACCEPTED, send a single modify (once) after configured delay
        if (
            not self._modified_sent
            and self._accepted_ts_ns is not None
            and (now_ns - self._accepted_ts_ns) >= int(self.config.modify_delay_secs * 1_000_000_000)
        ):
            order = self.cache.order(self._submitted_coid)
            if order is None:
                self.log.warning(
                    f"[SIMPLE] Cannot modify {self._submitted_coid}: order not in cache yet",
                    LogColor.BLUE,
                )
            else:
                inst = self.instrument or self.cache.instrument(self.config.instrument_id)
                if inst is None:
                    self.log.warning("[SIMPLE] Instrument not in cache; skipping modify", LogColor.BLUE)
                else:
                    mode = (self.config.modify_mode or "qty").lower()
                    new_qty: Quantity | None = None
                    new_px: Price | None = None

                    # --- Quantity modification ---
                    if mode in ("qty", "size", "both"):
                        size_step = inst.size_increment.as_decimal()
                        orig_qty_dec = order.quantity.as_decimal()
                        target_qty_dec = (orig_qty_dec * self.config.modify_size_factor).quantize(
                            size_step,
                            rounding=ROUND_UP,
                        )
                        if target_qty_dec != orig_qty_dec:
                            new_qty = Quantity.from_str(str(target_qty_dec))

                    # --- Price modification ---
                    if mode in ("price", "both"):
                        book = self.cache.order_book(self.config.instrument_id)
                        if book and book.spread():
                            best_bid = book.best_bid_price()
                            best_ask = book.best_ask_price()
                            px_step = inst.price_increment.as_decimal()
                            side = self.config.side.upper()
                            offset = Decimal(str(self.config.price_offset_pct))
                            # Move further away from the market to avoid fills, but ensure price differs.
                            if side == "BUY":
                                ref = best_bid.as_decimal()
                                raw_px = ref * (Decimal("1") - offset * Decimal("1.5"))
                            else:
                                ref = best_ask.as_decimal()
                                raw_px = ref * (Decimal("1") + offset * Decimal("1.5"))
                            steps = (raw_px / px_step).to_integral_value(rounding=ROUND_FLOOR)
                            px_dec = steps * px_step
                            orig_px_dec = order.price.as_decimal() if order.price is not None else None
                            if orig_px_dec is None or px_dec != orig_px_dec:
                                new_px = Price.from_str(str(px_dec))
                        else:
                            self.log.warning("[SIMPLE] No usable book for price modify; skipping price change", LogColor.BLUE)

                    if new_qty is None and new_px is None:
                        self.log.info(
                            f"[SIMPLE] Modify skipped: no effective change for {self._submitted_coid}",
                            LogColor.BLUE,
                        )
                    else:
                        self.log.info(
                            f"[SIMPLE] MODIFY {self._submitted_coid} "
                            f"qty {order.quantity} -> {new_qty or order.quantity}, "
                            f"px {order.price} -> {new_px or order.price}",
                            LogColor.BLUE,
                        )
                        self.modify_order(order, quantity=new_qty, price=new_px)
                        self._modified_sent = True

        # After delay, send cancel (once)
        if (
            not self._cancel_sent
            and self._submitted_ts_ns is not None
            and (now_ns - self._submitted_ts_ns) >= int(self.config.wait_before_cancel_secs * 1_000_000_000)
        ):
            if self.config.dry_run:
                self.log.warning("[SIMPLE] dry_run=True, skipping cancel")
                self._cancel_sent = True
                return
            self.log.info(
                f"[SIMPLE] Sending cancel for {self._submitted_coid}",
                LogColor.BLUE,
            )
            # Retrieve the live order from the cache and cancel it via Strategy API.
            order = self.cache.order(self._submitted_coid)
            if order is None:
                self.log.warning(
                    f"[SIMPLE] Cannot cancel {self._submitted_coid}: order not in cache yet",
                    LogColor.BLUE,
                )
            else:
                self.cancel_order(order)
            self._cancel_sent = True

    def _place_test_order(self, now_ns: int) -> None:
        # Ensure we know the instrument before placing orders.
        if not self._ensure_instrument():
            self.log.info("[SIMPLE] Instrument not yet available; cannot place test order", LogColor.BLUE)
            return

        # Prefer the current cached order book for best bid/ask.
        book = self.cache.order_book(self.config.instrument_id)
        best_bid: Price | None = None
        best_ask: Price | None = None
        if book and book.spread():
            best_bid = book.best_bid_price()
            best_ask = book.best_ask_price()
            self.log.info(
                f"[SIMPLE] Using book for test order best_bid={best_bid} best_ask={best_ask}",
                LogColor.BLUE,
            )
        elif self._last_trade_px is not None:
            # Fallback: use last trade price as a symmetric reference if no book yet.
            best_bid = self._last_trade_px
            best_ask = self._last_trade_px
            self.log.info(
                f"[SIMPLE] No book; using last trade price={self._last_trade_px} as reference",
                LogColor.BLUE,
            )
        else:
            self.log.info("[SIMPLE] No usable order book or trade price; skipping submit", LogColor.BLUE)
            return

        if best_bid is None or best_ask is None:
            self.log.info("[SIMPLE] Missing best bid/ask reference; skipping submit", LogColor.BLUE)
            return

        side = self.config.side.upper()
        px_step = self.instrument.price_increment.as_decimal()
        offset = Decimal(str(self.config.price_offset_pct))

        if side == "BUY":
            ref_px = best_bid.as_decimal()
            # Place below best bid to avoid immediate fill.
            raw_px = ref_px * (Decimal("1") - offset)
        else:
            ref_px = best_ask.as_decimal()
            # Place above best ask to avoid immediate fill.
            raw_px = ref_px * (Decimal("1") + offset)

        # Quantize to tick size
        steps = (raw_px / px_step).to_integral_value(rounding=ROUND_FLOOR)
        px_dec = steps * px_step

        # Quantity from config, adjusted to size increment and min_notional
        size_step = self.instrument.size_increment.as_decimal()
        qty_dec = Decimal(str(self.config.entry_qty))
        qty_steps = (qty_dec / size_step).to_integral_value(rounding=ROUND_UP)
        qty_dec = qty_steps * size_step

        if self.instrument.min_notional is not None and px_dec > 0:
            min_notional = self.instrument.min_notional.as_decimal()
            notional = px_dec * qty_dec
            if notional < min_notional:
                target_qty = (min_notional / px_dec).to_integral_value(rounding=ROUND_UP)
                qty_dec = target_qty * size_step
                self.log.warning(
                    f"[SIMPLE] notional {notional} < min_notional {min_notional}, "
                    f"bumping qty to {qty_dec}",
                )

        price = Price.from_str(str(px_dec))
        quantity = Quantity.from_str(str(qty_dec))
        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL

        order = self.order_factory.limit(
            instrument_id=self.instrument.id,
            order_side=order_side,
            quantity=quantity,
            price=price,
            time_in_force=TimeInForce.GTC,
            post_only=True,
        )
        self.log.info(f"[SIMPLE] SUBMIT order={order}", LogColor.BLUE)
        if self.config.dry_run:
            self.log.warning("[SIMPLE] dry_run=True, skipping submission")
            return

        self.submit_order(order)
        self._submitted_coid = order.client_order_id
        self._submitted_ts_ns = now_ns

    # --- Order lifecycle logging ----------------------------------------------------------

    def on_order_submitted(self, event: OrderSubmitted) -> None:
        self.log.info(
            f"[SIMPLE] OrderSubmitted {event.client_order_id} {event.instrument_id}",
            LogColor.BLUE,
        )

    def on_order_accepted(self, event: OrderAccepted) -> None:
        # Note: OrderAccepted events in Nautilus do not expose a direct
        # `order_status` attribute; status is implied by the event type itself.
        # Record acceptance time so we can drive a later modify step.
        if self._submitted_coid is not None and event.client_order_id == self._submitted_coid:
            self._accepted_ts_ns = event.ts_event
        self.log.info(
            f"[SIMPLE] OrderAccepted {event.client_order_id}",
            LogColor.BLUE,
        )

    def on_order_canceled(self, event: OrderCanceled) -> None:
        # OrderCanceled events do not carry a free-form reason attribute; the
        # event type itself indicates a successful cancellation.
        self.log.info(
            f"[SIMPLE] OrderCanceled {event.client_order_id}",
            LogColor.BLUE,
        )

    def on_order_pending_update(self, event: OrderPendingUpdate) -> None:
        self.log.info(
            f"[SIMPLE] OrderPendingUpdate {event.client_order_id}",
            LogColor.BLUE,
        )

    def on_order_updated(self, event: OrderUpdated) -> None:
        self.log.info(
            f"[SIMPLE] OrderUpdated {event.client_order_id}",
            LogColor.BLUE,
        )

    def on_order_modify_rejected(self, event: OrderModifyRejected) -> None:
        self.log.warning(
            f"[SIMPLE] OrderModifyRejected {event.client_order_id} reason={event.reason}",
        )

    def on_order_rejected(self, event: OrderRejected) -> None:
        self.log.warning(
            f"[SIMPLE] OrderRejected {event.client_order_id} reason={event.reason}",
        )

    # --- Data logging (OrderBook / TradeTick / MarketStats) -------------------------------

    def on_trade_tick(self, tick: TradeTick) -> None:
        if tick.instrument_id != self.config.instrument_id:
            return
        # Track last trade price as a fallback reference for test orders.
        self._last_trade_px = tick.price
        self.log.info(f"[SIMPLE] TradeTick px={tick.price} qty={tick.size}", LogColor.BLUE)

    def on_data(self, data) -> None:
        # Generic data tap: log data types and capture CustomData(LighterMarketStats).
        # This verifies that MarketStats CustomData flows from DataEngine into the strategy.
        if self._data_log_count < 10:
            self._data_log_count += 1
            self.log.info(f"[SIMPLE] on_data type={type(data).__name__}", LogColor.BLUE)

        if isinstance(data, CustomData) and isinstance(data.data, LighterMarketStats):
            ms = data.data
            self.log.info(
                f"[SIMPLE] MarketStats idx={ms.index_price} mark={ms.mark_price} oi={ms.open_interest}",
                LogColor.BLUE,
            )


def _base_from_symbol(symbol_value: str) -> str:
    # Assumes symbol of form BASEUSDC-PERP; this is true for current Lighter perp naming.
    if symbol_value.endswith("USDC-PERP"):
        return symbol_value.replace("USDC-PERP", "")
    return symbol_value


def _build_trading_node_config(
    instrument_id: InstrumentId,
    creds: LighterCredentials,
    http_url: str,
    ws_url: str,
) -> TradingNodeConfig:
    """
    Assemble a TradingNodeConfig for the simple place+cancel test.
    """
    exec_engine_cfg = LiveExecEngineConfig(
        reconciliation=True,
        inflight_check_interval_ms=2_000,
        inflight_check_threshold_ms=5_000,
        open_check_interval_secs=15.0,
        open_check_open_only=True,
        open_check_lookback_mins=60,
    )

    base = _base_from_symbol(instrument_id.symbol.value)
    prov_cfg = InstrumentProviderConfig(
        load_all=True,
        filters={"bases": [base]},
    )

    routing_cfg = RoutingConfig(
        default=True,
        venues=frozenset({LIGHTER_VENUE.value}),
    )

    data_clients = {
        "lighter": LighterDataClientConfig(
            base_url_http=http_url,
            base_url_ws=ws_url,
            instrument_provider=prov_cfg,
            routing=routing_cfg,
        ),
    }

    exec_clients = {
        "lighter": LighterExecClientConfig(
            base_url_http=http_url,
            base_url_ws=ws_url,
            chain_id=304,
            credentials=creds,
            instrument_provider=prov_cfg,
            routing=routing_cfg,
            subscribe_account_stats=False,
        ),
    }

    # Optional: select modify mode via environment for testing:
    #   LIGHTER_MODIFY_MODE=qty|price|both
    import os as _os
    modify_mode = _os.getenv("LIGHTER_MODIFY_MODE", "qty")

    strat_cfg = ImportableStrategyConfig(
        strategy_path=(
            "nautilus_trader.adapters.lighter.scripts.lighter_tests."
            "live_lighter_simple_place_cancel:LighterSimplePlaceCancelStrategy"
        ),
        config_path=(
            "nautilus_trader.adapters.lighter.scripts.lighter_tests."
            "live_lighter_simple_place_cancel:LighterSimplePlaceCancelConfig"
        ),
        config={
            "order_id_tag": "LIGHTER-SIMPLE-PC",
            "instrument_id": instrument_id.value,
            "entry_qty": "0.02",
            "side": "BUY",
            "price_offset_pct": 0.02,
            "wait_before_cancel_secs": 20.0,
            "modify_delay_secs": 5.0,
            "modify_size_factor": "1.5",
            "modify_mode": modify_mode,
            "dry_run": False,
        },
    )

    return TradingNodeConfig(
        trader_id="LIGHTER-TRADER-SIMPLE-PC",
        data_engine=LiveDataEngineConfig(),
        risk_engine=LiveRiskEngineConfig(),
        exec_engine=exec_engine_cfg,
        data_clients=data_clients,
        exec_clients=exec_clients,
        strategies=[strat_cfg],
        load_state=False,
        save_state=False,
    )


def main() -> int:
    http_url = os.getenv("LIGHTER_HTTP_URL", "https://mainnet.zklighter.elliot.ai").rstrip("/")
    ws_url = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")

    pub = os.getenv("LIGHTER_PUBLIC_KEY")
    priv = os.getenv("LIGHTER_PRIVATE_KEY")
    acct = os.getenv("LIGHTER_ACCOUNT_INDEX")
    api_idx = os.getenv("LIGHTER_API_KEY_INDEX", "2")

    if not (pub and priv and acct):
        print(
            "ERROR: missing LIGHTER_PUBLIC_KEY / LIGHTER_PRIVATE_KEY / LIGHTER_ACCOUNT_INDEX\n"
            "Please export them before running this script.",
        )
        return 1

    instrument_str = os.getenv("LIGHTER_INSTRUMENT_ID", "BNBUSDC-PERP.LIGHTER")
    instrument_id = InstrumentId.from_str(instrument_str)

    creds = LighterCredentials(
        pubkey=pub,
        account_index=int(acct),
        api_key_index=int(api_idx),
        private_key=priv,
    )

    node_config = _build_trading_node_config(
        instrument_id=instrument_id,
        creds=creds,
        http_url=http_url,
        ws_url=ws_url,
    )

    node = TradingNode(config=node_config)
    node.add_data_client_factory("lighter", LighterLiveDataClientFactory)
    node.add_exec_client_factory("lighter", LighterLiveExecClientFactory)
    node.build()

    run_secs = int(os.getenv("LIGHTER_LIVE_DEMO_SECS", "60"))
    print(f"[SIMPLE-PC] Starting TradingNode for ~{run_secs}s on {instrument_id}…")
    print(f"[SIMPLE-PC] HTTP={http_url}  WS={ws_url}")

    loop = node.get_event_loop()
    if loop is None:
        print("ERROR: TradingNode has no event loop")
        return 1

    loop.call_later(run_secs, node.stop)

    try:
        node.run()
    finally:
        node.dispose()
        print("[SIMPLE-PC] Done.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
