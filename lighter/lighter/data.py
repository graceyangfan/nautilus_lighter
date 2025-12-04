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

import asyncio
from typing import Any, Callable
import os

import msgspec

from nautilus_trader.adapters.lighter.config import LighterDataClientConfig, LighterRateLimitConfig
from nautilus_trader.adapters.lighter.common.constants import LIGHTER_VENUE
from nautilus_trader.adapters.lighter.common.types import LighterMarketStats
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.schemas.ws import (
    LighterWsEnvelope,
    LighterWsMarketStatsMsg,
    LighterWsMarketStatsAllMsg,
    LighterWsOrderBookMsg,
    LighterWsOrderBookSubscribedMsg,
    LighterWsTradeMsg,
)
from nautilus_trader.adapters.lighter.websocket.client import LighterWebSocketClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.data.messages import SubscribeData
from nautilus_trader.data.messages import UnsubscribeData
from nautilus_trader.data.messages import (
    RequestInstrument,
    RequestInstruments,
    SubscribeInstrument,
    SubscribeInstruments,
    UnsubscribeInstrument,
    UnsubscribeInstruments,
)
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.data.messages import SubscribeOrderBook, SubscribeTradeTicks
from nautilus_trader.data.messages import UnsubscribeOrderBook, UnsubscribeTradeTicks
from nautilus_trader.model.data import CustomData, OrderBookDeltas, TradeTick
from nautilus_trader.core.data import Data
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.core.datetime import millis_to_nanos


class LighterDataClient(LiveMarketDataClient):
    """
    Lighter public market data client.

    Supports streaming `market_stats/all` as a single CustomData stream using
    `LighterMarketStats`.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        http: LighterPublicHttpClient,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: LighterInstrumentProvider,
        base_url_ws: str | None = None,
        name: str | None = None,
        config: LighterDataClientConfig | None = None,
    ) -> None:
        client_name = name or LIGHTER_VENUE.value

        ratelimit = config.ratelimit if isinstance(config, LighterDataClientConfig) else None
        ws_kwargs: dict[str, Any] = {}
        if isinstance(ratelimit, LighterRateLimitConfig):
            ws_kwargs = {
                "ws_send_quota_per_sec": ratelimit.ws_send_per_second,
                "reconnect_timeout_ms": ratelimit.ws_reconnect_timeout_ms,
                "reconnect_delay_initial_ms": ratelimit.ws_reconnect_delay_initial_ms,
                "reconnect_delay_max_ms": ratelimit.ws_reconnect_delay_max_ms,
                "reconnect_backoff_factor": ratelimit.ws_reconnect_backoff_factor,
                "reconnect_jitter_ms": ratelimit.ws_reconnect_jitter_ms,
            }

        ws_url = base_url_ws or (config.base_url_ws if isinstance(config, LighterDataClientConfig) else None)
        if ws_url is None:
            raise ValueError("base_url_ws must be provided either via argument or config")

        super().__init__(
            loop=loop,
            client_id=ClientId(client_name),
            venue=LIGHTER_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
        )
        PyCondition.not_none(http, "http")
        self._http = http
        self._ratelimit = ratelimit
        self._ws = LighterWebSocketClient(
            clock=clock,
            base_url=ws_url,
            handler=self._handle_ws_message,
            handler_reconnect=self._on_ws_reconnect,
            loop=loop,
            **ws_kwargs,
        )
        self._decoder_envelope = msgspec.json.Decoder(LighterWsEnvelope)
        self._decoder_market_stats = msgspec.json.Decoder(LighterWsMarketStatsMsg)
        self._decoder_order_book = msgspec.json.Decoder(LighterWsOrderBookMsg)
        self._decoder_order_book_sub = msgspec.json.Decoder(LighterWsOrderBookSubscribedMsg)
        self._decoder_market_stats_all = msgspec.json.Decoder(LighterWsMarketStatsAllMsg)
        self._decoder_trade = msgspec.json.Decoder(LighterWsTradeMsg)

        # Map of market_id -> instrument_id
        self._market_instrument: dict[int, InstrumentId] = {}

        # Stats subscriptions
        self._stats_subscriptions: set[InstrumentId] = set()
        self._stats_all_subscribed: bool = False
        # Order book subscription state
        self._book_depths: dict[InstrumentId, int | None] = {}
        # Trades: track which instruments subscribed
        self._trade_subscriptions: set[InstrumentId] = set()

        # WebSocket dispatch tables (exact channel and prefix handlers).
        self._ws_exact_handlers: dict[str, Callable[[bytes, LighterWsEnvelope], None]] = {
            "market_stats:all": self._ws_handle_market_stats_all,
        }
        # Order matters for prefix matching.
        self._ws_prefix_handlers: list[tuple[str, Callable[[bytes, LighterWsEnvelope], None]]] = [
            ("market_stats:", self._ws_handle_market_stats),
            ("order_book:", self._ws_handle_order_book),
            ("trade:", self._ws_handle_trade),
        ]

    # --- Data forwarding -------------------------------------------------------------------------

    def _handle_data(self, data: Data) -> None:
        """
        Forward processed data to the DataEngine, with optional debug logging.

        When the environment variable ``LIGHTER_DATA_DEBUG`` is set, a compact
        summary of key data types (CustomData/LighterMarketStats, OrderBookDeltas,
        TradeTick) is logged before forwarding. This helps verify that the
        LighterDataClient is receiving and parsing raw WS messages correctly.
        """
        if os.getenv("LIGHTER_DATA_DEBUG"):
            try:
                tname = type(data).__name__
                if isinstance(data, CustomData):
                    payload = data.data
                    if payload is not None and type(payload).__name__.endswith("LighterMarketStats"):
                        # market stats custom data
                        self._log.info(
                            f"[DATA-DEBUG] MarketStats inst={payload.instrument_id} "
                            f"index={payload.index_price} mark={payload.mark_price} oi={payload.open_interest}",
                            LogColor.BLUE,
                        )
                elif isinstance(data, OrderBookDeltas):
                    inst_id = data.instrument_id
                    deltas = data.deltas or []
                    self._log.info(
                        f"[DATA-DEBUG] OrderBookDeltas inst={inst_id} count={len(deltas)} "
                        f"clear={bool(deltas and deltas[0].is_clear)}",
                        LogColor.BLUE,
                    )
                elif isinstance(data, TradeTick):
                    self._log.info(
                        f"[DATA-DEBUG] TradeTick inst={data.instrument_id} px={data.price} qty={data.size}",
                        LogColor.BLUE,
                    )
            except Exception:
                # debug-only; never block data forwarding
                pass

        # Forward to DataEngine as usual via the base implementation
        super()._handle_data(data)

    # --- Helpers -------------------------------------------------------------------------------

    def _market_index_from_channel(self, channel: str, expected_prefix: str) -> int | None:
        # Expect format: f"{expected_prefix}:{MARKET_INDEX}"
        try:
            prefix, rest = channel.split(":", 1)
        except ValueError:
            self._log.debug(f"Malformed channel (no colon): {channel}")
            return None
        if prefix != expected_prefix or not rest:
            self._log.debug(f"Unexpected channel prefix for {channel}, expected '{expected_prefix}'")
            return None
        try:
            return int(rest)
        except ValueError:
            self._log.debug(f"Non-integer market index in channel: {channel}")
            return None

    def _resolve_instrument_id(self, market_index: int | None) -> InstrumentId | None:
        """Resolve an InstrumentId from a Lighter market index with caching.

        Attempts lookup in the local `market_index -> instrument_id` cache first,
        then falls back to the provider's `find_by_market_index` and backfills
        the cache when successful.

        Returns None when the market is unknown to the provider.
        """
        if market_index is None:
            return None
        inst_id = self._market_instrument.get(market_index)
        if inst_id is not None:
            return inst_id
        inst_id = self._instrument_provider.find_by_market_index(market_index)
        if inst_id is not None:
            self._market_instrument[market_index] = inst_id
        return inst_id

    def _build_market_mapping(self) -> None:
        self._market_instrument.clear()
        for inst in self._instrument_provider.get_all().values():
            info = inst.info
            market_id = int(info["market_id"])  # fail-fast if missing
            self._market_instrument[market_id] = inst.id
        self._log.debug(f"mapped markets={len(self._market_instrument)}", LogColor.BLUE)

    def _send_all_instruments_to_data_engine(self) -> None:
        for instrument in self._instrument_provider.get_all().values():
            self._handle_data(instrument)
        for currency in self._instrument_provider.currencies().values():
            self._cache.add_currency(currency)

    async def _on_ws_reconnect(self) -> None:
        # No-op: subscriptions are auto-flushed by the WS client after handshake
        self._log.debug("WS reconnect completed")

    # -- Subscriptions (LiveMarketDataClient overrides) ------------------------------------------

    async def _connect(self) -> None:
        # Ensure instruments are loaded so that order book subscriptions can resolve market_id.
        await self._instrument_provider.initialize()
        self._build_market_mapping()
        self._send_all_instruments_to_data_engine()
        await self._ws.connect()

    async def _disconnect(self) -> None:
        await self._ws.disconnect()

    async def _subscribe(self, command: SubscribeData) -> None:
        # CustomData subscription uses DataType metadata/type to match
        dt = command.data_type
        if dt.type == LighterMarketStats:
            if command.instrument_id is None:
                await self._ws.subscribe("market_stats/all")
                self._stats_all_subscribed = True
            else:
                mid = self._extract_market_id(command.instrument_id)
                if mid is None:
                    self._log.error(f"Cannot subscribe market_stats: no market_id for {command.instrument_id}")
                else:
                    self._stats_subscriptions.add(command.instrument_id)
                    await self._ws.subscribe(f"market_stats/{mid}")
            return

        # Order book deltas / snapshots
        if isinstance(command, SubscribeOrderBook):
            await self._subscribe_order_book(command)
            return

        # Trade ticks
        if isinstance(command, SubscribeTradeTicks):
            await self._subscribe_trade_ticks(command)
            return

        self._log.warning(f"Unsupported subscription for data type {dt}")

    async def _unsubscribe(self, command: UnsubscribeData) -> None:
        # Order book
        if isinstance(command, UnsubscribeOrderBook):
            inst_id = command.instrument_id
            mid = self._extract_market_id(inst_id)
            if mid is not None:
                self._book_depths.pop(inst_id, None)
                await self._ws.unsubscribe(f"order_book/{mid}")
            return

        # Trades
        if isinstance(command, UnsubscribeTradeTicks):
            inst_id = command.instrument_id
            mid = self._extract_market_id(inst_id)
            if mid is not None:
                self._trade_subscriptions.discard(inst_id)
                await self._ws.unsubscribe(f"trade/{mid}")
            return

        # Market stats via CustomData
        dt = command.data_type
        if dt.type == LighterMarketStats:
            if command.instrument_id is None:
                if self._stats_all_subscribed:
                    self._stats_all_subscribed = False
                    await self._ws.unsubscribe("market_stats/all")
            else:
                self._stats_subscriptions.discard(command.instrument_id)
                mid = self._extract_market_id(command.instrument_id)
                if mid is not None:
                    await self._ws.unsubscribe(f"market_stats/{mid}")
            return

        self._log.warning(f"Unsupported unsubscription for data type {dt}")

    # Bridge high-level DataClient API used by strategies to our async subscribe handlers.

    def subscribe_order_book_deltas(self, command: SubscribeOrderBook) -> None:  # type: ignore[override]
        self.create_task(
            self._subscribe_order_book(command),
            log_msg=f"subscribe_order_book_deltas {command.instrument_id}",
            success_msg=f"Subscribed order_book_deltas {command.instrument_id}",
            success_color=LogColor.BLUE,
        )

    def unsubscribe_order_book_deltas(self, command: UnsubscribeOrderBook) -> None:  # type: ignore[override]
        self.create_task(
            self._unsubscribe(command),
            log_msg=f"unsubscribe_order_book_deltas {command.instrument_id}",
            success_msg=f"Unsubscribed order_book_deltas {command.instrument_id}",
            success_color=LogColor.BLUE,
        )

    def subscribe_trade_ticks(self, command: SubscribeTradeTicks) -> None:  # type: ignore[override]
        self.create_task(
            self._subscribe_trade_ticks(command),
            log_msg=f"subscribe_trade_ticks {command.instrument_id}",
            success_msg=f"Subscribed trade_ticks {command.instrument_id}",
            success_color=LogColor.BLUE,
        )

    def unsubscribe_trade_ticks(self, command: UnsubscribeTradeTicks) -> None:  # type: ignore[override]
        self.create_task(
            self._unsubscribe(command),
            log_msg=f"unsubscribe_trade_ticks {command.instrument_id}",
            success_msg=f"Unsubscribed trade_ticks {command.instrument_id}",
            success_color=LogColor.BLUE,
        )

    # -- Requests --------------------------------------------------------------------------------
    async def _request_instrument(self, request: RequestInstrument) -> None:
        instrument = self._instrument_provider.find(request.instrument_id)
        if instrument is None:
            await self._instrument_provider.load_ids_async([request.instrument_id])
            instrument = self._instrument_provider.find(request.instrument_id)

        if instrument is None:
            self._log.error(f"Cannot find instrument for {request.instrument_id}")
            return

        market_id = instrument.info.get("market_id")  # type: ignore[union-attr]
        if isinstance(market_id, int):
            self._market_instrument[market_id] = instrument.id

        self._handle_instrument(
            instrument,
            request.id,
            request.start,
            request.end,
            request.params,
        )

    async def _request_instruments(self, request: RequestInstruments) -> None:
        instruments_map: dict[InstrumentId, Any] = {}
        missing: list[InstrumentId] = []
        for inst_id in request.instrument_ids:
            inst = self._instrument_provider.find(inst_id)
            if inst is None:
                missing.append(inst_id)
            else:
                market_id = inst.info.get("market_id")  # type: ignore[union-attr]
                if isinstance(market_id, int):
                    self._market_instrument[market_id] = inst.id
                instruments_map[inst.id] = inst

        if missing:
            await self._instrument_provider.load_ids_async(missing)
            for inst_id in missing:
                inst = self._instrument_provider.find(inst_id)
                if inst is None:
                    self._log.warning(f"Instrument not found: {inst_id}")
                    continue
                market_id = inst.info.get("market_id")  # type: ignore[union-attr]
                if isinstance(market_id, int):
                    self._market_instrument[market_id] = inst.id
                instruments_map[inst.id] = inst

        # Return instruments in a single batch with correlation ID, consistent with other adapters.
        self._handle_instruments(
            request.venue,
            instruments_map,
            request.id,
            request.start,
            request.end,
            request.params,
        )

    # -- Instrument subscribe/unsubscribe --------------------------------------------------------

    async def _subscribe_instrument(self, command: SubscribeInstrument) -> None:
        # No dedicated WS instrument stream for Lighter; acknowledge subscription
        self._log.debug(f"Subscribed to instrument updates for {command.instrument_id}")

    async def _subscribe_instruments(self, command: SubscribeInstruments) -> None:
        self._log.debug("Subscribed to instruments updates")

    async def _unsubscribe_instrument(self, command: UnsubscribeInstrument) -> None:
        self._log.debug(f"Unsubscribed from instrument updates for {command.instrument_id}")

    async def _unsubscribe_instruments(self, command: UnsubscribeInstruments) -> None:
        self._log.debug("Unsubscribed from instruments updates")

    # -- WS Handling -----------------------------------------------------------------------------

    def _handle_ws_message(self, raw: bytes) -> None:
        # Decode envelope once, then dispatch via tables
        env = self._decoder_envelope.decode(raw)
        channel = env.channel or ""
        # Exact channel match
        handler = self._ws_exact_handlers.get(channel)
        if handler is not None:
            handler(raw, env)
            return
        # Prefix matches (in order)
        for prefix, cb in self._ws_prefix_handlers:
            if channel.startswith(prefix):
                cb(raw, env)
                return
        # Unknown message shape for current subscriptions: ignore silently
        return

    # --- WS dispatch wrappers --------------------------------------------------------------------
    def _ws_handle_market_stats_all(self, raw: bytes, env: LighterWsEnvelope) -> None:
        msg = self._decoder_market_stats_all.decode(raw)
        self._on_market_stats_all_msg(msg)

    def _ws_handle_market_stats(self, raw: bytes, env: LighterWsEnvelope) -> None:
        msg = self._decoder_market_stats.decode(raw)
        self._on_market_stats_msg(msg)

    def _ws_handle_order_book(self, raw: bytes, env: LighterWsEnvelope) -> None:
        msg_type = (env.type or "")
        if msg_type == "subscribed/order_book":
            msg = self._decoder_order_book_sub.decode(raw)
            # Optional WS-level debug: confirm snapshot depth per side
            if os.getenv("LIGHTER_BOOK_DEBUG"):
                asks = msg.order_book.asks or []
                bids = msg.order_book.bids or []
                self._log.info(
                    f"[BOOK-WS] snapshot {msg.channel} bids={len(bids)} asks={len(asks)}",
                    LogColor.BLUE,
                )
            self._on_order_book_snapshot_msg(msg)
        else:
            msg = self._decoder_order_book.decode(raw)
            if os.getenv("LIGHTER_BOOK_DEBUG"):
                asks = msg.order_book.asks or []
                bids = msg.order_book.bids or []
                self._log.info(
                    f"[BOOK-WS] update {msg.channel} bids={len(bids)} asks={len(asks)} "
                    f"offset={msg.offset}",
                    LogColor.BLUE,
                )
            self._on_order_book_msg(msg)

    def _ws_handle_trade(self, raw: bytes, env: LighterWsEnvelope) -> None:
        msg = self._decoder_trade.decode(raw)
        self._on_trade_msg(msg)

    # --- Typed WS handlers ---------------------------------------------------------------------
    def _on_market_stats_msg(self, msg: LighterWsMarketStatsMsg) -> None:
        stats = msg.market_stats
        inst_id = self._resolve_instrument_id(stats.market_id)
        if inst_id is None:
            return
        if (not self._stats_all_subscribed) and self._stats_subscriptions and inst_id not in self._stats_subscriptions:
            return
        now = self._clock.timestamp_ns()
        self._handle_data(stats.parse_to_custom_data(inst_id, now))

    def _on_market_stats_all_msg(self, msg: LighterWsMarketStatsAllMsg) -> None:
        if msg.channel != "market_stats:all" or not isinstance(msg.market_stats, dict):
            return
        now = self._clock.timestamp_ns()
        for _, payload in msg.market_stats.items():
            inst_id = self._resolve_instrument_id(payload.market_id)
            if inst_id is None:
                continue
            if (not self._stats_all_subscribed) and self._stats_subscriptions and inst_id not in self._stats_subscriptions:
                continue
            self._handle_data(payload.parse_to_custom_data(inst_id, now))

    def _on_order_book_msg(self, msg: LighterWsOrderBookMsg) -> None:
        market_index = self._market_index_from_channel(msg.channel, "order_book")
        inst_id = self._resolve_instrument_id(market_index)
        if inst_id is None:
            return
        ts = self._clock.timestamp_ns()
        self._handle_data(msg.parse_to_deltas(inst_id, ts))

    def _on_order_book_snapshot_msg(self, msg: LighterWsOrderBookSubscribedMsg) -> None:
        market_index = self._market_index_from_channel(msg.channel, "order_book")
        inst_id = self._resolve_instrument_id(market_index)
        if inst_id is None:
            return
        ts = self._clock.timestamp_ns()
        self._handle_data(msg.parse_to_deltas(inst_id, ts))

    def _on_trade_msg(self, msg: LighterWsTradeMsg) -> None:
        market_index = self._market_index_from_channel(msg.channel, "trade")
        inst_id = self._resolve_instrument_id(market_index)
        if inst_id is None:
            return
        if self._trade_subscriptions and inst_id not in self._trade_subscriptions:
            return
        ts_init = self._clock.timestamp_ns()
        for t in msg.trades or []:
            ts_event = millis_to_nanos(t.timestamp) if t.timestamp is not None else ts_init
            price = Price.from_str(t.price)
            size = Quantity.from_str(t.size)
            if t.is_maker_ask is True:
                aggressor = AggressorSide.BUYER
            elif t.is_maker_ask is False:
                aggressor = AggressorSide.SELLER
            else:
                aggressor = AggressorSide.UNKNOWN
            trade = TradeTick(
                instrument_id=inst_id,
                price=price,
                size=size,
                aggressor_side=aggressor,
                trade_id=TradeId(str(t.trade_id)),
                ts_event=ts_event,
                ts_init=ts_init,
            )
            self._handle_data(trade)

    # -- Order Book helpers ----------------------------------------------------------------------

    async def _subscribe_order_book(self, command: SubscribeOrderBook) -> None:
        instrument_id = command.instrument_id
        market_id = self._extract_market_id(instrument_id)
        if market_id is None:
            self._log.error(f"Cannot subscribe order_book: no market_id for {instrument_id}")
            return
        self._book_depths[instrument_id] = command.depth
        # Subscribe WS; server delivers a full snapshot via 'subscribed/order_book'
        await self._ws.subscribe(f"order_book/{market_id}")

    def _extract_market_id(self, instrument_id: InstrumentId) -> int | None:
        inst = self._instrument_provider.get_all().get(instrument_id)
        if not inst:
            return None
        info = inst.info
        return int(info["market_id"])  # fail-fast if missing

    # Snapshot arrives via WS 'subscribed/order_book'; no REST snapshot is triggered here.

    async def _subscribe_trade_ticks(self, command: SubscribeTradeTicks) -> None:
        inst_id = command.instrument_id
        mid = self._extract_market_id(inst_id)
        if mid is None:
            self._log.error(f"Cannot subscribe trades: no market_id for {inst_id}")
            return
        self._trade_subscriptions.add(inst_id)
        await self._ws.subscribe(f"trade/{mid}")
