# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

from typing import Any, Iterable, Iterator

import msgspec

from nautilus_trader.core.datetime import millis_to_nanos
from nautilus_trader.model.data import (
    CustomData,
    DataType,
    OrderBookDelta,
    OrderBookDeltas,
    BookOrder,
    TradeTick,
)
from nautilus_trader.model.enums import OrderSide, BookAction, RecordFlag, AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.adapters.lighter.common.types import LighterMarketStats
from nautilus_trader.adapters.lighter.common.normalize import to_float as _norm_to_float


# ---- Normalization helpers for private account frames -------------------------------------------

def _flatten_dict_of_lists(obj: Any) -> list[Any] | Any:
    if isinstance(obj, dict):
        out: list[Any] = []
        for v in obj.values():
            if isinstance(v, list):
                out.extend(v)
            else:
                out.append(v)
        return out
    return obj


def normalize_account_all_root(obj: Any) -> dict[str, Any]:
    """Normalize various private account shapes to a single root dict.

    - Flattens `data` wrapper into root.
    - Leaves `orders` / `trades` as dict-of-lists (schema expects this shape).
    - Normalizes `positions` into the canonical mapping form expected by
      ``LighterWsAccountAllRootStrict``, while preserving the exact venue
      structure whenever possible.
    """
    if not isinstance(obj, dict):
        return {}
    root = dict(obj)
    data = root.get("data")
    if isinstance(data, dict):
        tmp = dict(root)
        tmp.pop("data", None)
        tmp.update(data)
        root = tmp

    # orders/trades: keep as dict (schema expects dict[str, list])
    orders = root.get("orders")
    trades = root.get("trades")

    # positions: accept either dict[str, dict] (canonical) or alternate shapes
    positions = root.get("positions")
    norm_positions: dict[str, Any] | None = None

    def _build_pos_dict(src: dict[str, Any]) -> dict[str, Any] | None:
        """Build a canonical LighterAccountPositionStrict-compatible dict."""
        mid = src.get("market_id", src.get("market_index"))
        if mid is None:
            return None
        try:
            mid_int = int(mid)
        except Exception:
            return None
        return {
            "market_id": mid_int,
            "position": str(src.get("position", src.get("size", "0"))),
            "avg_entry_price": src.get("avg_entry_price"),
            "position_value": src.get("position_value"),
            "initial_margin_fraction": src.get("initial_margin_fraction"),
            "unrealized_pnl": src.get("unrealized_pnl"),
            "symbol": src.get("symbol"),
            "open_order_count": src.get("open_order_count"),
            "pending_order_count": src.get("pending_order_count"),
            "position_tied_order_count": src.get("position_tied_order_count"),
            "sign": src.get("sign"),
            "realized_pnl": src.get("realized_pnl"),
            "liquidation_price": src.get("liquidation_price"),
            "margin_mode": src.get("margin_mode"),
            "allocated_margin": src.get("allocated_margin"),
        }

    if isinstance(positions, dict):
        # If values already look like canonical position objects (market_id + position),
        # keep them as-is so they decode directly into LighterAccountPositionStrict.
        sample = next(iter(positions.values()), None)
        if isinstance(sample, dict) and "market_id" in sample and "position" in sample:
            norm_positions = positions
        else:
            tmp: dict[str, Any] = {}
            for key, val in positions.items():
                if not isinstance(val, dict):
                    continue
                pos_dict = _build_pos_dict(val)
                if pos_dict is None:
                    continue
                k = str(pos_dict["market_id"]) if "market_id" in pos_dict else str(key)
                tmp[k] = pos_dict
            if tmp:
                norm_positions = tmp
    elif isinstance(positions, list):
        tmp2: dict[str, Any] = {}
        for val in positions:
            if not isinstance(val, dict):
                continue
            pos_dict = _build_pos_dict(val)
            if pos_dict is None:
                continue
            k = str(pos_dict["market_id"])
            tmp2[k] = pos_dict
        if tmp2:
            norm_positions = tmp2

    root["orders"] = orders
    root["trades"] = trades
    if norm_positions is not None:
        root["positions"] = norm_positions
    return root


def normalize_account_all_bytes(raw: bytes) -> bytes:
    """Normalize bytes for private account frames to a single strict root encoding."""
    try:
        obj = msgspec.json.decode(raw)
    except msgspec.DecodeError:
        return raw
    norm = normalize_account_all_root(obj)
    return msgspec.json.encode(norm)


class LighterMarketStatsPayload(msgspec.Struct, frozen=True, omit_defaults=True):
    market_id: int
    index_price: str | None = None
    mark_price: str | None = None
    open_interest: str | None = None
    last_trade_price: str | None = None
    current_funding_rate: str | None = None
    funding_rate: str | None = None
    funding_timestamp: int | None = None
    daily_base_token_volume: float | None = None
    daily_quote_token_volume: float | None = None
    daily_price_low: float | None = None
    daily_price_high: float | None = None
    daily_price_change: float | None = None

    def parse_to_custom_data(self, instrument_id: InstrumentId, now_ns: int) -> CustomData:
        data = LighterMarketStats(
            instrument_id=instrument_id,
            ts_event=now_ns,
            ts_init=now_ns,
            index_price=_norm_to_float(self.index_price),
            mark_price=_norm_to_float(self.mark_price),
            open_interest=_norm_to_float(self.open_interest),
            last_trade_price=_norm_to_float(self.last_trade_price),
            current_funding_rate=_norm_to_float(self.current_funding_rate),
            funding_rate=_norm_to_float(self.funding_rate),
            funding_timestamp=self.funding_timestamp,
            daily_base_token_volume=self.daily_base_token_volume,
            daily_quote_token_volume=self.daily_quote_token_volume,
            daily_price_low=self.daily_price_low,
            daily_price_high=self.daily_price_high,
            daily_price_change=self.daily_price_change,
        )
        return CustomData(DataType(LighterMarketStats, {"instrument_id": instrument_id}), data)


class LighterWsEnvelope(msgspec.Struct, frozen=True, omit_defaults=True):
    """Minimal envelope for routing WS messages by channel/type."""

    channel: str | None = None
    type: str | None = None


class LighterWsMarketStatsMsg(msgspec.Struct, frozen=True, omit_defaults=True):
    channel: str
    market_stats: LighterMarketStatsPayload
    type: str


def is_market_stats_msg(obj: Any) -> bool:
    if isinstance(obj, LighterWsMarketStatsMsg):
        return obj.channel.startswith("market_stats:") and obj.type.endswith("market_stats")
    return False


class LighterOrderBookLevel(msgspec.Struct, frozen=True):
    price: str
    size: str


class LighterOrderBookPayload(msgspec.Struct, frozen=True, omit_defaults=True):
    code: int | None = None
    asks: list[LighterOrderBookLevel] = []
    bids: list[LighterOrderBookLevel] = []
    offset: int | None = None

def is_order_book_msg(obj: Any) -> bool:
    if isinstance(obj, LighterWsOrderBookMsg):
        return obj.channel.startswith("order_book:") and obj.type.endswith("order_book")
    return False


def is_order_book_subscribed_msg(obj: Any) -> bool:
    if isinstance(obj, LighterWsOrderBookSubscribedMsg):
        return obj.channel.startswith("order_book:") and obj.type == "subscribed/order_book"
    return False


# Trades -----------------------------------------------------------------------------------------

class LighterTrade(msgspec.Struct, frozen=True, omit_defaults=True):
    trade_id: int | None = None
    market_id: int | None = None
    size: str | None = None
    price: str | None = None
    tx_hash: str | None = None
    type: str | None = None
    usd_amount: str | None = None
    ask_id: int | None = None
    bid_id: int | None = None
    ask_account_id: int | None = None
    bid_account_id: int | None = None
    is_maker_ask: bool | None = None
    block_height: int | None = None
    timestamp: int | None = None

    def parse_to_trade_tick(self, instrument_id: InstrumentId, ts_init_ns: int) -> TradeTick:
        ts_event = millis_to_nanos(self.timestamp) if self.timestamp is not None else ts_init_ns
        if self.is_maker_ask is True:
            aggressor = AggressorSide.BUYER
        elif self.is_maker_ask is False:
            aggressor = AggressorSide.SELLER
        else:
            aggressor = AggressorSide.UNKNOWN
        return TradeTick(
            instrument_id=instrument_id,
            price=Price.from_str(self.price or "0"),
            size=Quantity.from_str(self.size or "0"),
            aggressor_side=aggressor,
            trade_id=TradeId(str(self.trade_id) if self.trade_id is not None else ""),
            ts_event=ts_event,
            ts_init=ts_init_ns,
        )


class LighterWsTradeMsg(msgspec.Struct, frozen=True, omit_defaults=True):
    channel: str
    trades: list[LighterTrade]
    type: str


def is_trade_msg(obj: Any) -> bool:
    if isinstance(obj, LighterWsTradeMsg):
        return obj.channel.startswith("trade:") and obj.type.endswith("update/trade")
    return False


# Private account streams (best-effort, optional fields) ------------------------------------------

"""Strict-only account messages used by the live parser.

Execution path normalizes historical variants into a single root shape (see
``normalize_account_all_bytes``) and then decodes using
``LighterWsAccountAllRootStrict``. Older compatibility structs for alternate
shapes have been removed to avoid confusion; the normalization step provides
compatibility for recorded frames.
"""

# --- Strict account messages ------------------------------------------------------

class LighterAccountOrderStrict(msgspec.Struct, frozen=True):
    # Required fields first (msgspec requirement)
    order_index: int
    market_index: int
    status: str
    is_ask: bool
    # Optional fields afterwards
    client_order_index: int | None = None
    base_amount: str | None = None
    remaining_base_amount: str | None = None
    executed_base_amount: str | None = None
    filled_size: str | None = None
    price: str | None = None
    avg_execution_price: str | None = None
    reduce_only: bool | None = None
    trigger_price: str | None = None

    def parse_to_order_status_report(
        self,
        account_id,
        instrument_id,
        client_order_id,
        venue_order_id,
        enum_parser,
        ts_init: int,
    ):
        """Parse to OrderStatusReport.

        Notes
        -----
        - Lighter account snapshots (HTTP / WS) do not currently expose full
          Nautilus `OrderType` / `TimeInForce` semantics. For external orders
          we therefore use best-effort defaults:
            * LIMIT if a price is present, otherwise MARKET.
            * GTC as the canonical time-in-force.
        - This is sufficient for execution reconciliation and aligns with how
          other adapters treat external orders (status-only, no lifecycle
          events).
        """
        from decimal import Decimal

        from nautilus_trader.execution.reports import OrderStatusReport
        from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce
        from nautilus_trader.model.objects import Quantity, Price
        from nautilus_trader.core.uuid import UUID4

        order_status = enum_parser.parse_order_status(self.status)
        order_side = OrderSide.SELL if self.is_ask else OrderSide.BUY

        # Best-effort order type inference for *external* orders.
        # Notes
        # -----
        # - This method is only used from `_process_account_orders` when there is
        #   no local strategy mapping (external orders, e.g. GUI-placed).
        # - For reconciliation purposes we do not attempt to reconstruct full
        #   STOP/TAKE_PROFIT semantics, as lighter snapshots do not expose the
        #   complete trigger configuration in a venue-agnostic way.
        # - To avoid creating invalid STOP_LIMIT/STOP_MARKET orders (which would
        #   fail core validations such as trigger_type != NO_TRIGGER), we treat
        #   external orders as plain LIMIT when a price is present, otherwise
        #   MARKET. The `trigger_price` field is ignored for external orders.
        has_price = bool(self.price)
        has_trigger = False
        if has_price:
            order_type = OrderType.LIMIT
        else:
            order_type = OrderType.MARKET

        time_in_force = TimeInForce.GTC

        # Quantity / filled quantity (must be non-null for OrderStatusReport)
        qty_str = (
            self.base_amount
            or self.remaining_base_amount
            or self.executed_base_amount
            or self.filled_size
            or "0"
        )
        filled_str = (self.executed_base_amount or self.filled_size or "0")

        quantity = Quantity.from_str(qty_str)
        filled_qty = Quantity.from_str(filled_str)

        avg_px_dec: Decimal | None = None
        if self.avg_execution_price is not None:
            try:
                avg_px_dec = Decimal(self.avg_execution_price)
            except Exception:
                avg_px_dec = None

        return OrderStatusReport(
            account_id=account_id,
            instrument_id=instrument_id,
            venue_order_id=venue_order_id,
            order_side=order_side,
            order_type=order_type,
            time_in_force=time_in_force,
            order_status=order_status,
            quantity=quantity,
            filled_qty=filled_qty,
            report_id=UUID4(),
            ts_accepted=ts_init,
            ts_last=ts_init,
            ts_init=ts_init,
            client_order_id=client_order_id,
            price=Price.from_str(self.price) if self.price else None,
            trigger_price=None,
            avg_px=avg_px_dec,
            reduce_only=bool(self.reduce_only) if self.reduce_only is not None else False,
        )


class LighterAccountTradeStrict(msgspec.Struct, frozen=True, omit_defaults=True):
    trade_id: int | str
    market_id: int
    price: str
    size: str
    ask_id: int
    bid_id: int
    ask_account_id: int
    bid_account_id: int
    is_maker_ask: bool
    block_height: int
    timestamp: int
    tx_hash: str | None = None
    type: str | None = None
    usd_amount: str | None = None
    ask_client_id: int | None = None
    bid_client_id: int | None = None
    taker_fee: int | None = None
    taker_position_size_before: str | None = None
    taker_entry_quote_before: str | None = None
    taker_initial_margin_fraction_before: int | None = None
    taker_position_sign_changed: bool | None = None
    maker_fee: int | None = None
    maker_position_size_before: str | None = None
    maker_entry_quote_before: str | None = None
    maker_initial_margin_fraction_before: int | None = None
    maker_position_sign_changed: bool | None = None
    order_index: int | None = None
    is_ask: bool | None = None
    is_maker: bool | None = None

    def parse_to_fill_report(
        self,
        account_id,
        instrument_id,
        client_order_id,
        venue_order_id,
        venue_position_id,
        is_our_maker: bool,
        order_side,
        commission,
        ts_init: int,
    ):
        """Parse to FillReport."""
        from nautilus_trader.execution.reports import FillReport
        from nautilus_trader.model.enums import LiquiditySide, OrderSide
        from nautilus_trader.model.identifiers import TradeId
        from nautilus_trader.model.objects import Quantity, Price
        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.core.datetime import millis_to_nanos

        liquidity_side = LiquiditySide.MAKER if is_our_maker else LiquiditySide.TAKER
        ts_event = millis_to_nanos(self.timestamp) if self.timestamp else ts_init

        return FillReport(
            account_id=account_id,
            instrument_id=instrument_id,
            venue_order_id=venue_order_id,
            trade_id=TradeId(str(self.trade_id)),
            order_side=order_side,
            last_qty=Quantity.from_str(self.size),
            last_px=Price.from_str(self.price),
            commission=commission,
            liquidity_side=liquidity_side,
            report_id=UUID4(),
            ts_event=ts_event,
            ts_init=ts_init,
            venue_position_id=venue_position_id,
            client_order_id=client_order_id,
        )


class LighterAccountPositionStrict(msgspec.Struct, frozen=True):
    market_id: int
    position: str  # signed base amount
    avg_entry_price: str | None = None
    position_value: str | None = None
    initial_margin_fraction: str | None = None
    unrealized_pnl: str | None = None
    symbol: str | None = None
    open_order_count: int | None = None
    pending_order_count: int | None = None
    position_tied_order_count: int | None = None
    sign: int | None = None
    realized_pnl: str | None = None
    liquidation_price: str | None = None
    margin_mode: int | None = None
    allocated_margin: str | None = None

    def parse_to_position_status_report(
        self,
        account_id,
        instrument_id,
        venue_position_id,
        ts_init: int,
    ):
        """Parse to PositionStatusReport."""
        from nautilus_trader.execution.reports import PositionStatusReport
        from nautilus_trader.model.enums import PositionSide
        from nautilus_trader.model.objects import Quantity, Price
        from nautilus_trader.core.uuid import UUID4
        from decimal import Decimal

        size_decimal = Decimal(self.position)
        if size_decimal > 0:
            position_side = PositionSide.LONG
        elif size_decimal < 0:
            position_side = PositionSide.SHORT
        else:
            position_side = PositionSide.FLAT

        return PositionStatusReport(
            account_id=account_id,
            instrument_id=instrument_id,
            position_side=position_side,
            quantity=Quantity.from_str(str(abs(size_decimal))),
            report_id=UUID4(),
            ts_last=ts_init,
            ts_init=ts_init,
            venue_position_id=venue_position_id,
        )


class LighterAccountAllDataStrict(msgspec.Struct, frozen=True):
    orders: list[LighterAccountOrderStrict]
    trades: list[LighterAccountTradeStrict]
    positions: list[LighterAccountPositionStrict]


"""Root-level strict account messages only (data-wrapper variant removed)."""

# Note: Historical data-wrapper struct LighterWsAccountAllStrict has been removed
# to enforce a single strict protocol shape aligned with lighter-python and
# common exchange styles.

    # (Removed older data-wrapper struct; normalization flattens to root.)


class LighterWsAccountAllRootStrict(msgspec.Struct, frozen=True, omit_defaults=True):
    """Strict root-level variant; tolerates omitted arrays and dict-maps for orders.

    Notes
    -----
    - Some channels (e.g. `account_all_orders`) may omit `trades`/`positions`.
    - Servers may emit `orders` as an object-map keyed by IDs; values remain strict.
    - On some deployments, private account messages omit the `type` field entirely.
      Make `type` optional to accept these frames.
    """

    channel: str
    type: str | None = None
    orders: dict[str, list[LighterAccountOrderStrict]] | None = None
    trades: dict[str, list[LighterAccountTradeStrict]] | None = None
    positions: dict[str, LighterAccountPositionStrict] | None = None
    shares: list | None = None


# Alternate strict variants observed on some deployments ------------------------------------------

    # (Removed alternate position struct; handled by normalization.)


    # (Removed older alt root struct; normalization flattens shapes.)


class LighterAccountStatsStrict(msgspec.Struct, frozen=True):
    available_balance: float
    total_balance: float


class LighterWsAccountStatsMsg(msgspec.Struct, frozen=True, omit_defaults=True):
    """Root-level strict account-stats message (no `data` wrapper).

    Balance fields may be encoded as numbers or numeric strings; execution
    will coerce to float.
    """

    channel: str | None = None
    type: str | None = None
    available_balance: float | str | int | None = None
    total_balance: float | str | int | None = None
class LighterWsMarketStatsAllMsg(msgspec.Struct, frozen=True, omit_defaults=True):
    channel: str
    market_stats: dict[str, LighterMarketStatsPayload]
    type: str | None = None

    def iter_custom_data(self, id_map: dict[int, InstrumentId], now_ns: int) -> Iterator[CustomData]:
        # keys of market_stats may be strings; rely on payload.market_id
        if not isinstance(self.market_stats, dict):
            return iter(())
        for payload in self.market_stats.values():
            inst_id = id_map.get(payload.market_id)
            if inst_id is None:
                continue
            yield payload.parse_to_custom_data(inst_id, now_ns)

    def parse_to_custom_data_list(self, id_map: dict[int, InstrumentId], now_ns: int) -> list[CustomData]:
        return list(self.iter_custom_data(id_map, now_ns))

# ---- sendTx results (WS) -----------------------------------------------------------------------

class LighterSendTxData(msgspec.Struct, frozen=True, omit_defaults=True):
    # status can be string or boolean; keep as object for coercion downstream
    status: object | None = None
    code: int | None = None
    tx_hash: str | None = None
    client_order_index: int | None = None
    order_index: int | None = None
    error: str | None = None
    message: str | None = None
    reason: str | None = None


class LighterWsSendTxResult(msgspec.Struct, frozen=True, omit_defaults=True):
    type: str | None = None
    channel: str | None = None
    code: int | None = None
    data: LighterSendTxData | None = None
    tx_hash: str | None = None


class LighterSendTxBatchData(msgspec.Struct, frozen=True, omit_defaults=True):
    results: list[LighterSendTxData] | None = None


class LighterWsSendTxBatchResult(msgspec.Struct, frozen=True, omit_defaults=True):
    type: str | None = None
    channel: str | None = None
    code: int | None = None
    data: LighterSendTxBatchData | None = None
    tx_hash: list[str] | None = None

# ---- parse helpers for order book ---------------------------------------------------------------

def _levels_to_deltas_update(
    inst_id: InstrumentId,
    sequence: int,
    levels: Iterable[LighterOrderBookLevel] | None,
    is_ask: bool,
    ts_ns: int,
) -> list[OrderBookDelta]:
    deltas: list[OrderBookDelta] = []
    if not levels:
        return deltas
    for lvl in levels:
        price = Price.from_str(lvl.price)
        size = Quantity.from_str(lvl.size)
        action = BookAction.UPDATE if size.raw > 0 else BookAction.DELETE
        # Map `is_ask` flag to the correct OrderSide; this is critical so that
        # the core `OrderBook` builder maintains bid/ask sides correctly.
        side = OrderSide.SELL if is_ask else OrderSide.BUY
        order = None if action == BookAction.DELETE else BookOrder(side, price, size, 0)
        deltas.append(
            OrderBookDelta(
                inst_id,
                action,
                order,
                flags=0,
                sequence=sequence,
                ts_event=ts_ns,
                ts_init=ts_ns,
            )
        )
    return deltas


def _levels_to_deltas_snapshot(
    inst_id: InstrumentId,
    levels: Iterable[LighterOrderBookLevel] | None,
    is_ask: bool,
    ts_ns: int,
    mark_last: bool = False,
) -> list[OrderBookDelta]:
    deltas: list[OrderBookDelta] = []
    if not levels:
        return deltas
    items = list(levels)
    for idx, lvl in enumerate(items):
        side = OrderSide.SELL if is_ask else OrderSide.BUY
        order = BookOrder(side, Price.from_str(lvl.price), Quantity.from_str(lvl.size), 0)
        flags = RecordFlag.F_LAST if (mark_last and idx == len(items) - 1) else 0
        deltas.append(
            OrderBookDelta(
                inst_id,
                BookAction.ADD,
                order,
                flags=flags,
                sequence=0,
                ts_event=ts_ns,
                ts_init=ts_ns,
            )
        )
    return deltas


def _clear_delta(inst_id: InstrumentId, ts_ns: int) -> OrderBookDelta:
    return OrderBookDelta.clear(inst_id, 0, ts_ns, ts_ns)

class LighterWsOrderBookMsg(msgspec.Struct, frozen=True, omit_defaults=True):
    channel: str
    offset: int
    order_book: LighterOrderBookPayload
    type: str

    def parse_to_deltas(self, instrument_id: InstrumentId, ts_ns: int) -> OrderBookDeltas:
        asks = self.order_book.asks or []
        bids = self.order_book.bids or []
        deltas: list[OrderBookDelta] = []
        deltas += _levels_to_deltas_update(instrument_id, self.offset, bids, False, ts_ns)
        deltas += _levels_to_deltas_update(instrument_id, self.offset, asks, True, ts_ns)
        return OrderBookDeltas(instrument_id, deltas)


class LighterWsOrderBookSubscribedMsg(msgspec.Struct, frozen=True, omit_defaults=True):
    channel: str
    order_book: LighterOrderBookPayload
    type: str

    def parse_to_deltas(self, instrument_id: InstrumentId, ts_ns: int) -> OrderBookDeltas:
        asks = self.order_book.asks or []
        bids = self.order_book.bids or []
        deltas: list[OrderBookDelta] = [_clear_delta(instrument_id, ts_ns)]
        deltas += _levels_to_deltas_snapshot(instrument_id, bids, False, ts_ns)
        deltas += _levels_to_deltas_snapshot(instrument_id, asks, True, ts_ns, mark_last=True)
        return OrderBookDeltas(instrument_id, deltas)
