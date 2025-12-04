#!/usr/bin/env python3
from __future__ import annotations

"""
Minimal live TradingNode example wired to the Lighter adapter.

What this script does
---------------------
- Builds a `TradingNode` with:
  - `LighterDataClient` (public market data).
  - `LighterExecutionClient` (private account / orders, with reconciliation enabled).
  - A very simple `SubscribeStrategy` (from Nautilus examples) which just subscribes
    to data and logs it.
- Registers the Lighter factories with the node so that `TradingNodeBuilder` can
  construct the clients.
- Starts the full live kernel (including `LiveExecutionEngine`), lets it run for a
  limited duration, then stops and disposes cleanly.

This is meant to show an end‑to‑end "real" live run where:
- `LiveExecutionEngine` drives reconciliation using
  `generate_order_status_reports(...)` / `generate_position_status_reports(...)`;
- `LighterExecutionClient` is used via the normal engine, not via ad‑hoc scripts.

Requirements
------------
- Environment variables for your Lighter mainnet API key:

  export LIGHTER_PUBLIC_KEY="..."
  export LIGHTER_PRIVATE_KEY="..."
  export LIGHTER_ACCOUNT_INDEX=XXXXX
  export LIGHTER_API_KEY_INDEX=2

- Optional:

  export LIGHTER_HTTP_URL="https://mainnet.zklighter.elliot.ai"
  export LIGHTER_WS_URL="wss://mainnet.zklighter.elliot.ai/stream"

  # JSONL recording (HTTP / WS), same mechanism as other test scripts
  export LIGHTER_HTTP_RECORD=/tmp/lighter_http_live_node.jsonl
  export LIGHTER_EXEC_RECORD=/tmp/lighter_exec_ws_live_node.jsonl

- Instrument:
  By default we use BNB perpetuals:

    BNBUSDC-PERP.LIGHTER

  You can override via:

    export LIGHTER_INSTRUMENT_ID="BNBUSDC-PERP.LIGHTER"

Run example
-----------

  PYTHONPATH=/root/myenv/lib/python3.12/site-packages \\
  python adapters/lighter/scripts/lighter_tests/live_lighter_node_subscribe.py

The node will:
- connect data + execution clients,
- complete initial reconciliation,
- run for ~60 seconds (configurable via LIGHTER_LIVE_DEMO_SECS),
- then shut down gracefully.
"""

import os
from collections import deque
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
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import BookType, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.events import PositionOpened, PositionChanged
from nautilus_trader.trading.config import ImportableStrategyConfig
from nautilus_trader.trading.strategy import Strategy


class LighterImbalanceTpConfig(StrategyConfig, frozen=True):
    """
    Configuration for ``LighterImbalanceTpStrategy``.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument to trade (e.g. BNBUSDC-PERP.LIGHTER).
    entry_qty : Decimal, default 0.02
        Target entry quantity (strategy will respect min_notional / min_quantity).
    imbalance_threshold : float, default 0.65
        Minimum side notional share required to consider an entry. In this
        short-only variant it is interpreted为“卖盘占比阈值”
        （即 bid_notional / (bid+ask) <= 1 - imbalance_threshold）。
    imbalance_zscore_threshold : float, default 1.0
        Minimum absolute z-score of imbalance vs recent history required to
        trigger. 在当前短空版本中，我们要求 z-score 明显为负。
    zscore_window : int, default 20
        Rolling window size for imbalance z-score.
    min_seconds_between_entries : float, default 30.0
        Minimum time between entry attempts.
    book_depth_levels : int, default 10
        Number of book levels to use when computing notional imbalance.
    tp_pct : float, default 0.0005
        Take-profit markup over entry price (0.0005 ~= 0.05%).
    dry_run : bool, default False
        If True, no orders will actually be submitted.
    """

    instrument_id: InstrumentId
    entry_qty: Decimal = Decimal("0.02")
    imbalance_threshold: float = 0.65
    imbalance_zscore_threshold: float = 1.0
    zscore_window: int = 20
    min_seconds_between_entries: float = 30.0
    book_depth_levels: int = 10
    tp_pct: float = 0.0005
    dry_run: bool = False


class LighterImbalanceTpStrategy(Strategy):
    """
    Lighter 订单簿不平衡 + 止盈 策略（当前测试版本为“只做空”）。

    行为概览
    --------
    - 订阅单一合约的 L2 OrderBook；
    - 在每次 OrderBook 更新时，计算：
      * 前 `book_depth_levels` 档买卖盘的名义金额；
      * 不平衡度 `imbalance = bid_notional / (bid_notional + ask_notional)`；
      * 不平衡度的滚动 z-score；
    - 当前版本只做空：
      * 当卖盘明显占优（imbalance 很低，即卖盘名义金额远大于买盘）
      * 且 z-score 显著为负（持续卖压，而非一次性噪音），
        在空仓且没有在途订单时，挂被动卖出限价单（开空）；
    - 收到 `PositionOpened` / `PositionChanged` 时，根据平均开仓价下
      一张反向的微利止盈 maker 单；
    - 当仓位完全平仓 (`PositionClosed`) 后，本次运行不再开新仓，
      方便我们做单次端到端回合测试。
    """

    def __init__(self, config: LighterImbalanceTpConfig) -> None:
        super().__init__(config)
        self.instrument: Instrument | None = None
        self.book_type: BookType = BookType.L2_MBP
        self._imbalance_history: deque[float] = deque(maxlen=config.zscore_window)
        self._last_entry_ns: int | None = None
        self._tp_active: bool = False
        # Once we observe a full round-trip (position opened then closed) we
        # can optionally stop sending new entry orders for the remainder of the
        # run. For tests which start with an existing external position, this
        # flag remains False so that the strategy can still bootstrap a TP.
        self._stop_trading: bool = False

    def on_start(self) -> None:
        # Request instrument via DataEngine first so it becomes available in cache.
        self.request_instrument(self.config.instrument_id)
        # Instrument may not yet be in cache at start; pick it up lazily in `_maybe_enter_long`.
        self.instrument = self.cache.instrument(self.config.instrument_id)
        self.book_type = BookType.L2_MBP
        self.subscribe_order_book_deltas(self.config.instrument_id, self.book_type)
        self.log.info(f"[IMB] Started for {self.config.instrument_id}", LogColor.BLUE)
        # If we start with an existing position (for example, external GUI
        # orders filled before the adapter started), attempt to bootstrap a
        # small take-profit order based on the current net position. This
        # allows realistic tests on top of live account state (non-zero
        # initial position + existing external orders).
        self._maybe_bootstrap_existing_position()

    def on_order_book_deltas(self, deltas: OrderBookDeltas) -> None:
        if deltas.instrument_id != self.config.instrument_id:
            return
        self._maybe_enter_trade()

    def on_order_book(self, order_book: OrderBook) -> None:
        if order_book.instrument_id != self.config.instrument_id:
            return
        self._maybe_enter_trade()

    def _maybe_enter_trade(self) -> None:
        # Ensure instrument is available (may appear shortly after start).
        if self.instrument is None:
            self.instrument = self.cache.instrument(self.config.instrument_id)
            if self.instrument is None:
                self.log.warning(f"[IMB] instrument {self.config.instrument_id} not yet in cache")
                return

        # After a full round-trip (position opened and then closed) we stop
        # sending further entry orders for this script run.
        if self._stop_trading:
            return

        # Respect a minimum spacing between entries
        now_ns = self.clock.timestamp_ns()
        if self._last_entry_ns is not None:
            dt_secs = (now_ns - self._last_entry_ns) / 1_000_000_000
            if dt_secs < self.config.min_seconds_between_entries:
                return

        book = self.cache.order_book(self.config.instrument_id)
        if not book or not book.spread():
            return

        imbalance, best_bid, best_ask = self._compute_imbalance(book)
        if imbalance is None or best_bid is None or best_ask is None:
            return

        # 第一次看到有效 orderbook 时，强制做一次小仓位开空，用于端到端测试
        # （确保后续 PositionOpened / TP / PositionClosed 链路能够被覆盖）。
        if self._last_entry_ns is None:
            self.log.info(
                f"[IMB] first book snapshot, forcing initial SHORT entry "
                f"imb={imbalance:.4f} bid={best_bid} ask={best_ask}",
                LogColor.BLUE,
            )
            self._place_entry(OrderSide.SELL, best_bid, best_ask)
            self._last_entry_ns = now_ns
            return

        # Update z-score
        self._imbalance_history.append(float(imbalance))
        zscore = self._compute_zscore()

        if zscore is not None:
            z_str = f"{zscore:.3f}"
        else:
            z_str = "None"

        self.log.info(
            f"[IMB] imb={imbalance:.4f} z={z_str} bid={best_bid} ask={best_ask}",
        )

        # 正常短空信号：卖盘占优（bid 占比较低）且 z-score 显著为负。
        thr = Decimal(str(self.config.imbalance_threshold))
        if (
            imbalance <= (Decimal("1") - thr)
            and zscore is not None
            and zscore <= -self.config.imbalance_zscore_threshold
        ):
            self._place_entry(OrderSide.SELL, best_bid, best_ask)
            self._last_entry_ns = now_ns

    def _maybe_bootstrap_existing_position(self) -> None:
        """
        If the account already has a net position on this instrument when the
        strategy starts (for example, from external GUI orders), optionally
        place a small TP order in the opposite direction.

        This is useful for tests where we want to verify that the adapter and
        strategy behave correctly when starting from non-flat state, without
        forcing a fresh entry from the strategy itself.
        """
        if self._tp_active or self._stop_trading:
            return
        if self.instrument is None:
            self.instrument = self.cache.instrument(self.config.instrument_id)
            if self.instrument is None:
                return

        try:
            net = self.portfolio.net_position(self.config.instrument_id)
        except Exception:
            return

        from decimal import Decimal as _Dec  # local alias
        net_dec = _Dec(str(net))
        if net_dec == _Dec("0"):
            return

        # Resolve current open position (if any) to derive quantity and avg_px.
        try:
            positions = self.cache.positions_open(instrument_id=self.config.instrument_id)
        except Exception:
            positions = []
        pos = positions[0] if positions else None
        if pos is not None:
            qty_dec = pos.quantity.as_decimal()
            try:
                entry_px_dec = _Dec(str(pos.avg_px_open))
            except Exception:
                entry_px_dec = None
        else:
            qty_dec = abs(net_dec)
            entry_px_dec = None

        if qty_dec <= _Dec("0"):
            return

        # If we cannot obtain a reliable entry price, skip bootstrapping TP to
        # avoid placing arbitrary orders.
        if entry_px_dec is None:
            return

        from nautilus_trader.model.enums import PositionSide as _PosSide
        side = _PosSide.LONG if net_dec > _Dec("0") else _PosSide.SHORT

        # Build a synthetic PositionOpened-like event using the current
        # position snapshot, then route through the same TP helper.
        class _SyntheticPos:
            def __init__(self, instrument_id, side, qty_dec, avg_px_dec):
                self.instrument_id = instrument_id
                self.side = side
                from nautilus_trader.model.objects import Quantity as _Qty
                self.quantity = _Qty.from_str(str(qty_dec))
                self.avg_px_open = avg_px_dec

        synthetic = _SyntheticPos(self.config.instrument_id, side, qty_dec, entry_px_dec)
        self._place_tp_for_position(synthetic)

    def _compute_imbalance(
        self,
        book: OrderBook,
    ) -> tuple[Decimal | None, Price | None, Price | None]:
        bids = book.bids()
        asks = book.asks()
        if not bids or not asks:
            return None, None, None

        depth = max(1, int(self.config.book_depth_levels))
        bid_notional = Decimal("0")
        ask_notional = Decimal("0")

        for level in bids[:depth]:
            try:
                px = level.price.as_decimal()
                sz = Decimal(str(level.size()))
                bid_notional += px * sz
            except Exception:
                continue

        for level in asks[:depth]:
            try:
                px = level.price.as_decimal()
                sz = Decimal(str(level.size()))
                ask_notional += px * sz
            except Exception:
                continue

        total = bid_notional + ask_notional
        if total <= 0:
            return None, None, None

        imbalance = bid_notional / total
        best_bid = book.best_bid_price()
        best_ask = book.best_ask_price()
        return imbalance, best_bid, best_ask

    def _compute_zscore(self) -> float | None:
        if len(self._imbalance_history) < max(5, self.config.zscore_window // 2):
            return None

        values = list(self._imbalance_history)
        n = len(values)
        mean = sum(values) / n
        var = sum((x - mean) ** 2 for x in values) / n
        if var <= 0:
            return None
        std = var ** 0.5
        return (values[-1] - mean) / std

    def _place_entry(self, side: OrderSide, best_bid: Price, best_ask: Price) -> None:
        # Quantize price to tick size, stay on the passive side to be maker.
        tick = self.instrument.price_increment.as_decimal()
        if side == OrderSide.BUY:
            ref_dec = best_bid.as_decimal()
            steps = (ref_dec / tick).to_integral_value(rounding=ROUND_FLOOR)
        else:
            ref_dec = best_ask.as_decimal()
            steps = (ref_dec / tick).to_integral_value(rounding=ROUND_FLOOR)
        entry_px_dec = steps * tick

        # Base quantity from config, adjust to size increment and min_notional.
        qty_dec = Decimal(str(self.config.entry_qty))
        size_step = self.instrument.size_increment.as_decimal()
        qty_steps = (qty_dec / size_step).to_integral_value(rounding=ROUND_UP)
        qty_dec = qty_steps * size_step

        # Respect min_notional if available
        if self.instrument.min_notional is not None:
            min_notional = self.instrument.min_notional.as_decimal()
            notional = entry_px_dec * qty_dec
            if notional < min_notional and entry_px_dec > 0:
                target_qty = (min_notional / entry_px_dec).to_integral_value(rounding=ROUND_UP)
                qty_dec = target_qty * size_step
                self.log.warning(
                    f"[IMB] notional {notional} < min_notional {min_notional}, "
                    f"bumping qty to {qty_dec}",
                )

        price = Price.from_str(str(entry_px_dec))
        quantity = Quantity.from_str(str(qty_dec))

        order = self.order_factory.limit(
            instrument_id=self.instrument.id,
            order_side=side,
            quantity=quantity,
            price=price,
            time_in_force=TimeInForce.GTC,
            post_only=True,
        )
        self.log.info(f"[IMB] ENTRY side={side.name} order={order}", LogColor.BLUE)
        if self.config.dry_run:
            self.log.warning("[IMB] dry_run=True, skipping entry submission")
            return
        self.submit_order(order)

    def _place_tp_for_position(self, event: PositionOpened) -> None:
        """
        Common helper to place a micro take-profit order for a given open
        position. This is used both when the position is opened by the
        strategy itself (via `on_position_opened`) and when bootstrapping from
        an existing net position at startup.
        """
        # Only react to positions on our instrument.
        if event.instrument_id != self.config.instrument_id:
            return
        if self._tp_active:
            return
        if not self.instrument:
            return

        # Derive entry price and quantity
        entry_px_dec = Decimal(str(event.avg_px_open))
        qty_dec = event.quantity.as_decimal()

        # Use current order book to choose a maker TP side:
        # - LONG: maker SELL near/below best ask (above entry px).
        # - SHORT: maker BUY near/above best bid (below entry px).
        book = self.cache.order_book(self.config.instrument_id)
        best_bid = book.best_bid_price() if book else None
        best_ask = book.best_ask_price() if book else None

        tp_pct = Decimal(str(self.config.tp_pct))
        from nautilus_trader.core.rust.model import PositionSide as CorePositionSide  # type: ignore[import]
        if event.side == CorePositionSide.LONG:
            tp_raw = entry_px_dec * (Decimal("1") + tp_pct)
            if best_ask is not None:
                tp_raw = max(tp_raw, best_ask.as_decimal())
            tp_side = OrderSide.SELL
        else:
            tp_raw = entry_px_dec * (Decimal("1") - tp_pct)
            if best_bid is not None:
                tp_raw = min(tp_raw, best_bid.as_decimal())
            tp_side = OrderSide.BUY

        tick = self.instrument.price_increment.as_decimal()
        steps = (tp_raw / tick).to_integral_value(rounding=ROUND_FLOOR)
        tp_px_dec = steps * tick

        price = Price.from_str(str(tp_px_dec))
        quantity = Quantity.from_str(str(qty_dec))

        order = self.order_factory.limit(
            instrument_id=self.instrument.id,
            order_side=tp_side,
            quantity=quantity,
            price=price,
            time_in_force=TimeInForce.GTC,
            post_only=True,
        )
        self.log.info(f"[IMB] TP side={tp_side.name} order={order}", LogColor.BLUE)
        if self.config.dry_run:
            self.log.warning("[IMB] dry_run=True, skipping TP submission")
            return
        self.submit_order(order)
        self._tp_active = True

    def on_position_opened(self, event: PositionOpened) -> None:
        # For strategy-owned fills, rely on the position event (PositionOpened)
        # rather than raw trade WS so that the logic remains robust even if
        # trade messages are missing. This is the main path for TP placement
        # when the strategy itself opens the position.
        self._place_tp_for_position(event)

    def on_position_changed(self, event: PositionChanged) -> None:
        # When the position size or average entry price changes (e.g. scaled
        # in), refresh the TP according to the new `avg_px_open`. This keeps
        # the take-profit aligned with the true average entry, which is more
        # robust when using partial fills or multiple entries.
        if event.instrument_id != self.config.instrument_id:
            return
        # Cancel any existing TP orders for this instrument, then place a
        # fresh TP using the updated average entry price.
        try:
            self.cancel_all_orders(self.config.instrument_id)
        except Exception:
            pass
        # Reset flag so that _place_tp_for_position will actually submit.
        self._tp_active = False
        self._place_tp_for_position(event)

    def on_position_closed(self, event) -> None:
        # When we observe a full close on this instrument, mark the strategy as
        # finished for this run (no new entries). This keeps the node alive but
        # prevents additional trades, which is useful for focused round-trip tests.
        if event.instrument_id != self.config.instrument_id:
            return
        self._tp_active = False
        self._stop_trading = True
        self.log.info(
            f"[IMB] Position closed for {event.instrument_id}, "
            "stopping further entries for this run.",
            LogColor.BLUE,
        )


def _build_trading_node_config(
    instrument_id: InstrumentId,
    creds: LighterCredentials,
    http_url: str,
    ws_url: str,
) -> TradingNodeConfig:
    """
    Assemble a minimal TradingNodeConfig for a single-venue Lighter live run.

    - Data engine / risk engine use default live configs.
    - Exec engine enables reconciliation and periodic open-check.
    - Data / exec clients are Lighter-specific configs.
    - A single SubscribeStrategy is attached for the chosen instrument.
    """
    # Engine-level execution config (continuous reconciliation etc.)
    exec_engine_cfg = LiveExecEngineConfig(
        reconciliation=True,
        # Let engine drive periodic open-check & in-flight checks
        inflight_check_interval_ms=2_000,
        inflight_check_threshold_ms=5_000,
        open_check_interval_secs=10.0,
        open_check_open_only=True,
        open_check_lookback_mins=60,
        # Use default generate_missing_orders=True so NETTING positions can be reconciled
    )

    # Instrument provider: load only the base we care about to keep HTTP usage low
    # while still ensuring the BNBUSDC-PERP instrument is available for both data
    # and execution clients.
    prov_cfg = InstrumentProviderConfig(
        load_all=True,
        filters={"bases": [instrument_id.symbol.value.replace("USDC-PERP", "")]},
    )

    # Routing: register Lighter as default and for its venue.
    routing_cfg = RoutingConfig(
        default=True,
        venues=frozenset({LIGHTER_VENUE.value}),
    )

    # Data client config (public market data, depth/trades/etc.).
    data_clients = {
        "lighter": LighterDataClientConfig(
            base_url_http=http_url,
            base_url_ws=ws_url,
            instrument_provider=prov_cfg,
            routing=routing_cfg,
        ),
    }

    # Execution client config (private orders/account).
    exec_clients = {
        "lighter": LighterExecClientConfig(
            base_url_http=http_url,
            base_url_ws=ws_url,
            chain_id=304,  # mainnet chain id
            credentials=creds,
            instrument_provider=prov_cfg,
            routing=routing_cfg,
            subscribe_account_stats=False,
        ),
    }

    # Strategy: order-book imbalance + TP, implemented in this module.
    strat_cfg = ImportableStrategyConfig(
        strategy_path=(
            "nautilus_trader.adapters.lighter.scripts.lighter_tests."
            "live_lighter_node_subscribe:LighterImbalanceTpStrategy"
        ),
        config_path=(
            "nautilus_trader.adapters.lighter.scripts.lighter_tests."
            "live_lighter_node_subscribe:LighterImbalanceTpConfig"
        ),
        config={
            "order_id_tag": "LIGHTER-IMB-TP",
            "instrument_id": instrument_id.value,
            "entry_qty": "0.02",
            # Stronger filters for live testing: require both a pronounced
            # bid imbalance and a meaningful positive z-score so that entries
            # only occur during clear regime shifts.
            "imbalance_threshold": 0.65,
            "imbalance_zscore_threshold": 1.5,
            "zscore_window": 20,
            "min_seconds_between_entries": 30.0,
            "book_depth_levels": 10,
            "tp_pct": 0.0005,
            "dry_run": False,
        },
    )

    return TradingNodeConfig(
        trader_id="LIGHTER-TRADER-001",
        data_engine=LiveDataEngineConfig(),
        risk_engine=LiveRiskEngineConfig(),
        exec_engine=exec_engine_cfg,
        data_clients=data_clients,
        exec_clients=exec_clients,
        strategies=[strat_cfg],
        # No cache/message_bus databases configured here: everything is in-memory.
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

    # Build node config (engines + clients + strategy).
    node_config = _build_trading_node_config(
        instrument_id=instrument_id,
        creds=creds,
        http_url=http_url,
        ws_url=ws_url,
    )

    # Build TradingNode and register Lighter factories.
    node = TradingNode(config=node_config)
    node.add_data_client_factory("lighter", LighterLiveDataClientFactory)
    node.add_exec_client_factory("lighter", LighterLiveExecClientFactory)
    node.build()

    # How long to run the live node before shutting down (seconds).
    run_secs = int(os.getenv("LIGHTER_LIVE_DEMO_SECS", "60"))
    print(f"[LIVE-NODE] Starting TradingNode for ~{run_secs}s on {instrument_id}…")
    print(f"[LIVE-NODE] HTTP={http_url}  WS={ws_url}")

    # Schedule a graceful stop after the given duration on the nodes event loop.
    loop = node.get_event_loop()
    if loop is None:
        print("ERROR: TradingNode has no event loop")
        return 1

    loop.call_later(run_secs, node.stop)

    # Run the node (blocks until stopped), then dispose.
    try:
        node.run()
    finally:
        node.dispose()
        print("[LIVE-NODE] Done.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
