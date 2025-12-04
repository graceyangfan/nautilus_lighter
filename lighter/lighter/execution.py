from __future__ import annotations

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

import asyncio
from decimal import Decimal, ROUND_HALF_UP
import json as pyjson
import msgspec
import hashlib
from dataclasses import dataclass
import time
import os
from typing import Any, Callable, Awaitable
from weakref import WeakSet

from nautilus_trader.adapters.lighter.config import LighterExecClientConfig, LighterRateLimitConfig
from nautilus_trader.adapters.lighter.common.constants import LIGHTER_VENUE, LIGHTER_CLIENT_ID
from nautilus_trader.adapters.lighter.common.enums import (
    LighterEnumParser,
    LighterOrderType,
    LighterTimeInForce,
)
from nautilus_trader.adapters.lighter.common.nonce import NonceManager
from nautilus_trader.adapters.lighter.common.normalize import (
    parse_int as _norm_parse_int,
    to_decimal as _norm_to_decimal,
    to_float as _norm_to_float,
    money_to_decimal as _norm_money_to_decimal,
    normalize_price_to_int as _norm_normalize_price,
    normalize_size_to_int as _norm_normalize_size,
)
from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner
from nautilus_trader.adapters.lighter.common.auth import LighterAuthManager
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.adapters.lighter.common.utils import is_nonce_error as _is_nonce_error
from nautilus_trader.adapters.lighter.http.transaction import LighterTransactionHttpClient
from nautilus_trader.adapters.lighter.http.errors import LighterHttpError
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.websocket.client import LighterWebSocketClient
from nautilus_trader.adapters.lighter.transport import LighterTransportService
from nautilus_trader.adapters.lighter.schemas.ws import (
    LighterAccountPositionStrict,
    LighterAccountOrderStrict,
    LighterWsAccountAllRootStrict,
    LighterAccountStatsStrict,
    LighterWsEnvelope,
    LighterWsAccountStatsMsg,
    LighterSendTxData,
    LighterWsSendTxResult,
    LighterWsSendTxBatchResult,
    normalize_account_all_bytes,
)
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.core.nautilus_pyo3 import Quota
from nautilus_trader.execution.messages import (
    BatchCancelOrders,
    CancelAllOrders,
    CancelOrder,
    ModifyOrder,
    SubmitOrder,
    SubmitOrderList,
    GenerateOrderStatusReports,
    GenerateOrderStatusReport,
    GeneratePositionStatusReports,
    GenerateFillReports,
)
from nautilus_trader.execution.reports import FillReport, OrderStatusReport, PositionStatusReport
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId, StrategyId, TradeId, VenueOrderId
from nautilus_trader.model.objects import AccountBalance, Currency, Money, Price, Quantity
from nautilus_trader.model.orders import Order

_ENUM = LighterEnumParser()


@dataclass(slots=True)
class _PreparedCreateOrder:
    order: Order
    instrument_id: InstrumentId
    strategy_id: StrategyId
    coid_value: str
    market_index: int
    client_order_index: int
    base_amount: int
    price: int
    trigger_price: int
    order_type: LighterOrderType
    time_in_force: LighterTimeInForce
    reduce_only: bool
    is_ask: bool
    order_expiry: int


class LighterExecutionClient(LiveExecutionClient):
    """
    Lighter execution client (private WebSocket integration skeleton).

    Establishes authenticated connectivity to the Lighter venue and provides
    the plumbing required for order submission, cancellation, and account
    state reconciliation.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: InstrumentProvider,
        base_url_http: str,
        base_url_ws: str,
        credentials: LighterCredentials,
        config: LighterExecClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=LIGHTER_CLIENT_ID,
            venue=LIGHTER_VENUE,
            # Lighter uses a single net position per instrument → NETTING OMS.
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=None,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )

        self._ws_url = base_url_ws.rstrip("/")
        # Rate limit + retry wiring
        http_keyed_quotas = []
        http_default_quota = None
        retry_kwargs = {}
        ws_kwargs = {}
        rl = config.ratelimit
        if isinstance(rl, LighterRateLimitConfig):
            if rl.http_default_per_minute:
                http_default_quota = Quota.rate_per_minute(rl.http_default_per_minute)
            for ep, per_min in (rl.http_endpoint_per_minute or {}).items():
                http_keyed_quotas.append((f"lighter:{ep}", Quota.rate_per_minute(per_min)))
            retry_kwargs = dict(
                retry_initial_ms=rl.retry_initial_ms,
                retry_max_ms=rl.retry_max_ms,
                retry_backoff_factor=rl.retry_backoff_factor,
                max_retries=rl.max_retries,
            )
            ws_kwargs = dict(
                ws_send_quota_per_sec=rl.ws_send_per_second,
                reconnect_timeout_ms=rl.ws_reconnect_timeout_ms,
                reconnect_delay_initial_ms=rl.ws_reconnect_delay_initial_ms,
                reconnect_delay_max_ms=rl.ws_reconnect_delay_max_ms,
                reconnect_backoff_factor=rl.ws_reconnect_backoff_factor,
                reconnect_jitter_ms=rl.ws_reconnect_jitter_ms,
            )

        self._http_account = LighterAccountHttpClient(
            base_url_http.rstrip("/"),
            ratelimiter_quotas=http_keyed_quotas,
            ratelimiter_default_quota=http_default_quota,
            **retry_kwargs,
        )
        self._http_tx = LighterTransactionHttpClient(
            base_url_http.rstrip("/"),
            ratelimiter_quotas=http_keyed_quotas,
            ratelimiter_default_quota=http_default_quota,
            **retry_kwargs,
        )
        # Keep ws overrides for connect
        self._ws_config_overrides = ws_kwargs
        # Credentials & signer are explicitly provided 
        self._creds = credentials
        self._signer = LighterSigner(self._creds, base_url=base_url_http, chain_id=config.chain_id)
        self._auth = LighterAuthManager(self._signer)
        self._nonce = NonceManager()

        self._ws: LighterWebSocketClient | None = None
        # Optional raw recorder for diagnostics (JSONL), path via env LIGHTER_EXEC_RECORD
        self._record_path = os.getenv("LIGHTER_EXEC_RECORD")
        # Default quote currency (updated after instruments load); derive from provider
        try:
            self._quote_currency: Currency = instrument_provider.default_quote_currency()
        except Exception:
            self._quote_currency: Currency = Currency.from_str("USDC")
        self._subscribe_account_stats = bool(config.subscribe_account_stats)
        self._post_submit_http_reconcile = bool(config.post_submit_http_reconcile)
        self._post_submit_poll_attempts = int(config.post_submit_poll_attempts)
        self._post_submit_poll_interval_ms = int(config.post_submit_poll_interval_ms)
        # HTTP fallback is disabled by default to align with other adapters.
        # Config is a typed dataclass; access attribute directly for clarity.
        self._http_send_fallback = bool(config.http_send_fallback) if isinstance(config, LighterExecClientConfig) else False
        # Private WS uses the PyO3 client only.
        self._tasks: WeakSet[asyncio.Task[Any]] = WeakSet()
        # Strict decoders for private messages
        self._dec_account_all_root = msgspec.json.Decoder(LighterWsAccountAllRootStrict)
        self._dec_account_stats_msg = msgspec.json.Decoder(LighterWsAccountStatsMsg)
        self._dec_ws_envelope = msgspec.json.Decoder(LighterWsEnvelope)
        self._dec_ws_sendtx_result = msgspec.json.Decoder(LighterWsSendTxResult)
        self._dec_ws_sendtx_batch = msgspec.json.Decoder(LighterWsSendTxBatchResult)

        # WebSocket dispatch tables for private streams
        self._ws_type_handlers: dict[str, Callable[[bytes, LighterWsEnvelope], None]] = {
            "authenticated": self._ws_handle_authenticated,
            "jsonapi/sendtx": self._ws_handle_sendtx_envelope,
            "jsonapi/sendtx_result": self._ws_handle_sendtx_envelope,
            "jsonapi/sendtxbatch": self._ws_handle_sendtx_batch_envelope,
            "jsonapi/sendtxbatch_result": self._ws_handle_sendtx_batch_envelope,
        }
        self._ws_channel_prefix_handlers: list[tuple[str, Callable[[bytes, LighterWsEnvelope], None]]] = [
            ("account_all:", self._ws_handle_account_all_envelope),
            ("account_all_orders:", self._ws_handle_account_all_envelope),
            ("account_all_trades:", self._ws_handle_account_all_envelope),
            ("account_all_positions:", self._ws_handle_account_all_envelope),
            ("account_stats:", self._ws_handle_account_stats_envelope),
        ]

        # Mappings for correlating private updates (Lighter-specific)
        self._market_instrument: dict[int, InstrumentId] = {}
        self._client_order_index_by_coid: dict[str, int] = {}
        self._coid_by_client_order_index: dict[int, ClientOrderId] = {}
        self._venue_order_id_by_coid: dict[str, VenueOrderId] = {}
        self._coid_by_venue_order_index: dict[int, ClientOrderId] = {}
        # Track last tx action kind for diagnostics and rejection mapping.
        # Indexed by client_order_index (for creates) and order_index (for modify/cancel).
        self._last_tx_action_by_client_index: dict[int, str] = {}
        self._last_tx_action_by_order_index: dict[int, str] = {}

        # Position state tracking (for deduplication)
        # State tuple is (signed_size_decimal, avg_entry_price_decimal_or_None, last_seen_ts_ns)
        self._last_position_state: dict[int, tuple[Decimal, Decimal | None, int]] = {}

        # De-duplication sets
        self._accepted_coids: set[str] = set()
        self._canceled_coids: set[str] = set()

        # Trade de-duplication by trade_id (LRU semantics)
        self._trade_dedup_capacity: int = int(config.trade_dedup_capacity)
        self._trade_id_lru: dict[int, None] = {}

        # Optional short suppression window for position duplicates (ns)
        pdw_ms = config.position_dedup_suppress_window_ms
        self._position_dedup_suppress_window_ns: int | None = (
            int(pdw_ms) * 1_000_000 if pdw_ms is not None else None
        )

        # Position tracking (Lighter-specific fields for margin calculation)
        # Stores: position_value, initial_margin_fraction, unrealized_pnl
        self._positions: dict[int, dict] = {}
        # Set a simple account ID for routing
        self._set_account_id(AccountId(f"{LIGHTER_VENUE.value}-{self._creds.account_index}"))
        # Diagnostics: only enabled in debug mode and when LIGHTER_EXEC_LOG is set
        dbg_env = os.getenv("LIGHTER_DEBUG") or os.getenv("NAUTILUS_DEBUG")
        self._debug_mode = bool(dbg_env)
        self._diag_log_path = os.getenv("LIGHTER_EXEC_LOG")
        self._dlog("init:", "signer_available=", self._signer.available())
        # Transport service: centralize signing + nonce + WS/HTTP send
        self._transport = LighterTransportService(
            clock=self._clock,
            signer=self._signer,
            nonce=self._nonce,
            auth=self._auth,
            ws_getter=lambda: self._ws,
            http_tx=self._http_tx,
            http_account=self._http_account,
            http_send_fallback=self._http_send_fallback,
        )

    def _dlog(self, *parts: Any) -> None:
        if not self._debug_mode or not self._diag_log_path:
            return
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            line = " ".join(str(p) for p in parts)
            with open(self._diag_log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {line}\n")
        except Exception:
            # best-effort only
            pass

    # --- Cache/instrument helpers ----------------------------------------------------
    def _ensure_instrument_cached(self, inst_id: InstrumentId) -> Instrument | None:
        """Ensure the given instrument is present in the shared Cache.

        - Try cache first; if missing, attempt to load from provider map.
        - If found via provider, publish it to DataEngine so Cache can ingest it.
        """
        inst = self._cache.instrument(inst_id)
        if inst is not None:
            return inst
        provider_map = self._instrument_provider.get_all() if self._instrument_provider is not None else {}
        inst = provider_map.get(inst_id)
        if inst is not None:
            # Send to DataEngine for consistent ingestion
            self._msgbus.send(endpoint="DataEngine.process", msg=inst)
            return inst
        return None

    # Nonce helpers moved to transport; Execution no longer exposes next()/fetchers

    # Lifecycle -----------------------------------------------------------------
    async def _connect(self) -> None:
        # Align with core style: allow provider config to drive loading
        try:
            await self._instrument_provider.initialize()
        except Exception:
            self._log.warning("Instrument provider initialize failed; continuing with empty map")

        # Build market_index -> InstrumentId mapping
        self._market_instrument.clear()
        for inst in self._instrument_provider.get_all().values():
            info = inst.info
            mid = int(info.get("market_id"))
            self._market_instrument[mid] = inst.id
            # Update default quote currency (provider emits CryptoPerpetual)
            self._quote_currency = inst.quote_currency
        for k, v in self._market_instrument.items():
            self._dlog("map_entry:", k, str(v))

        # Initialize nonce from HTTP
        # Optional: preflight API key visibility for diagnostics only
        try:
            server_keys = await self._http_account.get_api_keys(
                account_index=self._creds.account_index,
                api_key_index=self._creds.api_key_index,
            )
            if server_keys:
                sv = server_keys[0]
                server_pub = sv.public_key or ""
                self._dlog("api_keys_server_pub:", server_pub[:24])
        except Exception:
            # best-effort only; continue
            pass

        try:
            n0 = await self._transport.refresh_nonce()
            self._dlog("nonce_init:", n0)
        except Exception:
            pass

        # Connect WS and authenticate
        self._ws = LighterWebSocketClient(
            clock=self._clock,
            base_url=self._ws_url,
            handler=self._on_ws_message,
            handler_reconnect=self._on_ws_reconnect,
            loop=self._loop,
            **(self._ws_config_overrides or {}),
        )
        await self._ws.connect()
        ready = await self._ws.wait_until_ready(10)
        if not ready:
            self._log.warning("Execution WS handshake did not report ready within timeout")
        self._dlog("connect: mapped=", len(self._market_instrument), "ws_ready=", ready)
        # Subscriptions are now handled strictly after the WS handshake by the
        # WebSocket client's `post_reconnection` hook (invokes our `_on_ws_reconnect`).
        # This avoids any duplicate or pre-handshake subscribe attempts.
        # Cold start: hydrate account snapshots via HTTP for reconciliation
        await self._fetch_and_emit_account_state_http()
        await self._fetch_and_emit_account_orders_http()

    async def _disconnect(self) -> None:
        if self._ws is not None:
            await self._ws.disconnect()
            self._ws = None

    # Internals ------------------------------------------------------------------
    # Lighter does not require a separate authenticate frame; private
    # subscriptions carry the auth token directly.

    # WS handlers ----------------------------------------------------------------
    def _on_ws_message(self, raw: bytes) -> None:
        env = self._dec_ws_envelope.decode(raw)
        msg_type = env.type or ""
        channel = env.channel or ""

        # Type-based handlers
        handler = self._ws_type_handlers.get(msg_type)
        if handler is not None:
            handler(raw, env)
            return

        # Channel-based prefix handlers
        for prefix, cb in self._ws_channel_prefix_handlers:
            if channel.startswith(prefix):
                cb(raw, env)
                return

        # Unknown: optionally record for diagnostics
        self._dlog("ws_unknown:", msg_type, channel)
        if self._record_path:
            try:
                obj = pyjson.loads(raw)
                self._record_jsonl("pyo3", obj)
            except Exception:
                pass
        return

    # --- Dispatch wrappers ------------------------------------------------------
    def _ws_handle_authenticated(self, raw: bytes, env: LighterWsEnvelope) -> None:
        # No-op placeholder; the PyO3 client already tracks authentication
        # state and we do not need to react to this frame explicitly.
        return

    def _ws_handle_sendtx_envelope(self, raw: bytes, env: LighterWsEnvelope) -> None:
        # Optional raw recording for diagnostics (single sendtx)
        if self._record_path:
            try:
                obj = pyjson.loads(raw)
                self._record_jsonl("pyo3", obj)
            except Exception:
                pass
        msg = self._dec_ws_sendtx_result.decode(raw)
        self._handle_sendtx_result(msg)

    def _ws_handle_sendtx_batch_envelope(self, raw: bytes, env: LighterWsEnvelope) -> None:
        # Optional raw recording for diagnostics (batch sendtx)
        if self._record_path:
            try:
                obj = pyjson.loads(raw)
                self._record_jsonl("pyo3", obj)
            except Exception:
                pass
        msg = self._dec_ws_sendtx_batch.decode(raw)
        self._handle_sendtx_batch_result(msg)

    def _ws_handle_account_all_envelope(self, raw: bytes, env: LighterWsEnvelope) -> None:
        # Optional raw recording
        if self._record_path:
            try:
                obj = pyjson.loads(raw)
                self._record_jsonl("pyo3", obj)
            except Exception:
                pass

        # Normalize variants to a single strict root, then decode once and
        # delegate to the typed account_all handler. All flattening into flat
        # order/trade/position lists is centralized in `_handle_account_all`
        # to avoid divergent code paths.
        try:
            norm = normalize_account_all_bytes(raw)
            root = self._dec_account_all_root.decode(norm)
            self._handle_account_all(root)
        except Exception as e:
            self._log.error(f"Failed to process account_all: {e}")
            return

    def _ws_handle_account_stats_envelope(self, raw: bytes, env: LighterWsEnvelope) -> None:
        stats_msg = self._dec_account_stats_msg.decode(raw)
        if stats_msg.total_balance is None and stats_msg.available_balance is None:
            return
        avail_f = _norm_to_float(stats_msg.available_balance, 0.0) or 0.0
        total_f = _norm_to_float(stats_msg.total_balance, avail_f) or avail_f
        strict_stats = LighterAccountStatsStrict(
            available_balance=avail_f,
            total_balance=total_f,
        )
        self._handle_account_stats(strict_stats)

    async def _on_ws_reconnect(self) -> None:  # noqa: D401
        """Resubscribe and refresh state after reconnect."""
        await self._subscribe_private_channels()
        await self._fetch_and_emit_account_state_http()
        await self._fetch_and_emit_account_orders_http()

    # Fallback dict handler (python websockets) ----------------------------------
    def _on_ws_message_dict(self, obj: dict[str, Any]) -> None:
        # Strict-only pathway: re-encode to JSON and delegate to the typed decoder.
        ch = str(obj.get("channel") or "")
        if self._record_path and (
            ch.startswith("account_all:")
            or ch.startswith("account_all_orders:")
            or ch.startswith("account_all_trades:")
            or ch.startswith("account_all_positions:")
        ):
            self._record_jsonl("pyws", obj)
        raw = msgspec.json.encode(obj)
        self._on_ws_message(raw)

    # --- Recorder ----------------------------------------------------------------
    def _record_jsonl(self, source: str, obj: Any) -> None:
        """Append a single JSON-serializable record to the diagnostics file."""
        try:
            line = pyjson.dumps({
                "ts": int(self._clock.timestamp_ns()),
                "src": source,
                "msg": obj,
            }, ensure_ascii=False)
            with open(self._record_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            # Best-effort only; recording must never affect core behaviour.
            pass

    # --- Minimal REST submission helpers --------------------------------------------
    async def send_tx(self, tx_type: int, tx_info: str, price_protection: bool | None = None) -> dict:
        return await self._http_tx.send_tx(tx_type=tx_type, tx_info=tx_info, price_protection=price_protection)

    # WS send helpers are provided by LighterWebSocketClient (jsonapi/sendtx[_batch])

    def _register_venue_mapping(self, coid: ClientOrderId, venue_order_id: VenueOrderId) -> None:
        self._venue_order_id_by_coid[coid.value] = venue_order_id
        vid = _norm_parse_int(venue_order_id.value)
        if vid is not None:
            self._coid_by_venue_order_index[vid] = coid
        # Also publish mapping into the shared Cache for cross-component lookups
        try:
            self._cache.add_venue_order_id(coid, venue_order_id, overwrite=True)
        except Exception:
            # Best-effort only; cache may not be ready in some isolated tests
            pass

    def _derive_market_price(
        self,
        instrument: Any,
        instrument_id: InstrumentId,
        price_precision: int,
        is_ask: bool,
    ) -> int | None:
        try:
            book = self._cache.order_book(instrument_id)
        except Exception:
            book = None
        if book is None:
            return None
        best_price = book.best_bid_price() if is_ask else book.best_ask_price()
        if best_price is None:
            return None
        price_int, _ = _norm_normalize_price(
            best_price,
            price_decimals=price_precision,
            price_increment=instrument.price_increment,
        )
        return price_int

    # ---- WS message handlers ---------------------------------------------------

    @staticmethod
    def _is_sendtx_ok(msg: LighterWsSendTxResult) -> tuple[bool, object | None]:
        """Determine whether a sendTx/sendTxBatch item should be treated as successful."""
        data = msg.data
        status = data.status if data is not None else None
        ok = False
        if isinstance(status, str):
            ok = status.lower() in ("ok", "success", "accepted")
        elif isinstance(status, bool):
            ok = bool(status)
        elif (data is not None and data.code == 200) or (msg.code == 200):
            ok = True
        return ok, status

    @staticmethod
    def _sendtx_error_text(data: LighterSendTxData | None) -> str | None:
        if data is None:
            return None
        return data.error or data.message or data.reason

    def _sendtx_resolve_coid(self, oi: int | None, coi: int | None) -> ClientOrderId | None:
        """Best-effort resolution of ClientOrderId from order_index / client_order_index."""
        coid: ClientOrderId | None = None
        if coi is not None:
            coid = self._coid_by_client_order_index.get(coi)
        if coid is None and oi is not None:
            coid = self._coid_by_venue_order_index.get(oi)
        if coid is not None:
            return coid
        # Fallback: scan venue_order_id mappings by value
        if oi is not None:
            for k, v in self._venue_order_id_by_coid.items():
                vi = _norm_parse_int(v.value)
                if vi is not None and vi == oi:
                    coid = ClientOrderId(k)
                    self._coid_by_venue_order_index[vi] = coid
                    return coid
        return None

    def _sendtx_dispatch_rejection(
        self,
        *,
        ok: bool,
        msg: LighterWsSendTxResult,
        err_text: str | None,
    ) -> None:
        """Emit OrderRejected / OrderModifyRejected based on the last tx action type."""
        data = msg.data
        if ok or data is None:
            return
        oi = _norm_parse_int(data.order_index)
        coi = _norm_parse_int(data.client_order_index)
        coid = self._sendtx_resolve_coid(oi, coi)
        if coid is None:
            return
        # Resolve instrument and strategy from cache/order; do not rely on
        # adapter-specific cache helpers which may not be present.
        try:
            strat = self._cache.strategy_id_for_order(coid)
        except Exception:
            strat = None
        order = None
        try:
            order = self._cache.order(coid)
        except Exception:
            order = None
        inst_id = order.instrument_id if order is not None else None
        if inst_id is None or strat is None:
            return
        reason = err_text or f"code={(data.code if data else None)}"
        # Determine last tx action kind (create/modify/cancel) for this order
        tx_kind: str | None = None
        if oi is not None:
            tx_kind = self._last_tx_action_by_order_index.get(oi)
        if tx_kind is None and coi is not None:
            tx_kind = self._last_tx_action_by_client_index.get(coi)
        if tx_kind == "modify":
            # Best-effort venue_order_id lookup
            venue_id = None
            try:
                venue_id = self._cache.venue_order_id_for_order(coid)
            except Exception:
                venue_id = self._venue_order_id_by_coid.get(coid.value)
            self.generate_order_modify_rejected(
                strategy_id=strat,
                instrument_id=inst_id,
                client_order_id=coid,
                venue_order_id=venue_id,
                reason=str(reason),
                ts_event=self._clock.timestamp_ns(),
            )
        else:
            self.generate_order_rejected(
                strategy_id=strat,
                instrument_id=inst_id,
                client_order_id=coid,
                reason=str(reason),
                ts_event=self._clock.timestamp_ns(),
            )

    def _handle_sendtx_result(self, msg: LighterWsSendTxResult) -> None:
        data = msg.data
        ok, status = self._is_sendtx_ok(msg)
        # Keep summary at debug level to avoid log spam in production; sendTx
        # errors are surfaced via OrderRejected/OrderModifyRejected.
        self._log.debug(f"sendtx_result ok={ok} data={data}")
        self._dlog("sendtx:", "ok=", ok, "code=", (data.code if data else None), "tx_hash=", (data.tx_hash if data else None))
        # Detect nonce errors and refresh local nonce (best-effort).
        err_text = self._sendtx_error_text(data)
        if _is_nonce_error(err_text):
            task = self._loop.create_task(self._transport.refresh_nonce())
            self._tasks.add(task)
            self._log.warning(
                "Detected nonce error; rolling back and refreshing nonce",
                LogColor.YELLOW,
            )
            self._dlog("nonce_error_refresh")
        # Emit rejection if possible (e.g., invalid signature) and we can correlate to a COI
        self._sendtx_dispatch_rejection(ok=ok, msg=msg, err_text=err_text)
        # If result includes order_index and client_order_index, pre-fill mapping
        if data is not None:
            oi = _norm_parse_int(data.order_index)
            coi = _norm_parse_int(data.client_order_index)
            if coi is not None and coi in self._coid_by_client_order_index:
                coid = self._coid_by_client_order_index.get(coi)
                if coid is not None and oi is not None:
                    self._register_venue_mapping(coid, VenueOrderId(str(oi)))

    def _handle_sendtx_batch_result(self, msg: LighterWsSendTxBatchResult) -> None:
        """Iterate results and handle each as a single sendtx result."""
        if msg.data and msg.data.results:
            for item in msg.data.results:
                # wrap into single-result structure for reuse
                self._handle_sendtx_result(
                    LighterWsSendTxResult(type=msg.type, channel=msg.channel, code=msg.code, data=item)
                )
            return
        # Fallback: if only tx_hash list is present, synthesize success results
        if msg.tx_hash:
            for h in msg.tx_hash:
                self._handle_sendtx_result(
                    LighterWsSendTxResult(data=LighterSendTxData(status="success", code=200, tx_hash=h))
                )

    def _handle_account_all(self, msg: LighterWsAccountAllRootStrict) -> None:
        """Flatten strict account_all root into flat lists and dispatch to processors."""
        orders_flat: list[LighterAccountOrderStrict] = []
        if msg.orders:
            for market_orders in msg.orders.values():
                if isinstance(market_orders, list):
                    orders_flat.extend(market_orders)

        trades_flat = []
        if msg.trades:
            for market_trades in msg.trades.values():
                trades_flat.extend(market_trades)

        positions_flat = []
        if msg.positions:
            for market_pos in msg.positions.values():
                positions_flat.append(market_pos)

        self._dlog(
            "acc_all:",
            "orders=", len(orders_flat),
            "trades=", len(trades_flat),
            "positions=", len(positions_flat),
        )
        self._process_account_orders(orders_flat)
        self._process_account_trades(trades_flat)
        self._process_account_positions(positions_flat)

    def _handle_account_stats(self, stats: LighterAccountStatsStrict) -> None:
        cur = self._quote_currency
        ts = self._clock.timestamp_ns()
        total_amt = Decimal(str(stats.total_balance))
        free_amt = Decimal(str(stats.available_balance))
        locked_amt = max(total_amt - free_amt, Decimal("0"))
        balances = [
            AccountBalance(total=Money(total_amt, cur), locked=Money(locked_amt, cur), free=Money(free_amt, cur)),
        ]
        self.generate_account_state(balances=balances, margins=[], reported=True, ts_event=ts, info=None)

    # --- Helper methods (common patterns) ---------------------------------------
    def _resolve_instrument_id(self, market_index: int) -> InstrumentId | None:
        """Resolve instrument_id from market_index, with provider fallback."""
        inst_id = self._market_instrument.get(market_index)
        if inst_id is None and isinstance(self._instrument_provider, LighterInstrumentProvider):
            inst_id = self._instrument_provider.find_by_market_index(market_index)
            if inst_id is not None:
                self._market_instrument[market_index] = inst_id
        return inst_id

    def _resolve_client_order_id(self, order_index: int, client_order_index: int | None) -> ClientOrderId | None:
        """Resolve ClientOrderId from order indices."""
        if client_order_index is not None:
            coid = self._coid_by_client_order_index.get(client_order_index)
            if coid:
                return coid

        coid = self._coid_by_venue_order_index.get(order_index)
        if coid:
            return coid

        # Fallback: scan venue_order_id mappings
        for k, v in self._venue_order_id_by_coid.items():
            vi = _norm_parse_int(v.value)
            if vi is not None and vi == order_index:
                coid = ClientOrderId(k)
                self._coid_by_venue_order_index[vi] = coid
                return coid
        return None

    def _find_our_order_id(self, trade: LighterAccountTradeStrict) -> str | None:
        """Find our order ID from trade (bid_id or ask_id)."""
        bid_id = str(trade.bid_id) if trade.bid_id else None
        ask_id = str(trade.ask_id) if trade.ask_id else None

        # Check bid_id
        if bid_id:
            vid = _norm_parse_int(bid_id)
            if vid is not None and vid in self._coid_by_venue_order_index:
                return bid_id

        # Check ask_id
        if ask_id:
            vid = _norm_parse_int(ask_id)
            if vid is not None and vid in self._coid_by_venue_order_index:
                return ask_id

        # Fallback: try client_order_index
        if trade.bid_client_id is not None:
            coi = _norm_parse_int(trade.bid_client_id)
            if coi is not None and coi in self._coid_by_client_order_index:
                coid_temp = self._coid_by_client_order_index.get(coi)
                if coid_temp is not None and bid_id:
                    vid = _norm_parse_int(bid_id)
                    if vid is not None:
                        self._register_venue_mapping(coid_temp, VenueOrderId(bid_id))
                        return bid_id

        if trade.ask_client_id is not None:
            coi = _norm_parse_int(trade.ask_client_id)
            if coi is not None and coi in self._coid_by_client_order_index:
                coid_temp = self._coid_by_client_order_index.get(coi)
                if coid_temp is not None and ask_id:
                    vid = _norm_parse_int(ask_id)
                    if vid is not None:
                        self._register_venue_mapping(coid_temp, VenueOrderId(ask_id))
                        return ask_id
        return None

    def _determine_maker_taker(self, trade: LighterAccountTradeStrict, our_order_id: str) -> tuple[bool, OrderSide]:
        """Determine if we are maker/taker and our order side."""
        bid_id = str(trade.bid_id) if trade.bid_id else None
        ask_id = str(trade.ask_id) if trade.ask_id else None

        if our_order_id == bid_id:
            return (not trade.is_maker_ask, OrderSide.BUY)
        elif our_order_id == ask_id:
            return (trade.is_maker_ask, OrderSide.SELL)
        else:
            return (False, OrderSide.BUY)

    def _is_duplicate_trade(self, trade_id: int | str) -> bool:
        """Check if trade is duplicate and update LRU cache."""
        try:
            tid_val = int(trade_id)
        except Exception:
            return False

        if tid_val in self._trade_id_lru:
            return True

        self._trade_id_lru[tid_val] = None
        if len(self._trade_id_lru) > self._trade_dedup_capacity:
            try:
                k = next(iter(self._trade_id_lru.keys()))
                if k is not None:
                    self._trade_id_lru.pop(k, None)
            except Exception:
                pass
        return False

    def _calculate_commission(self, trade: LighterAccountTradeStrict, is_maker: bool, quote_currency: Currency) -> Money:
        """Calculate commission for trade."""
        size = Decimal(trade.size)
        price = Decimal(trade.price)
        notional = size * price
        fee_rate = Decimal("0.00002") if is_maker else Decimal("0.0002")
        fee_amount = notional * fee_rate
        return Money(fee_amount, quote_currency)

    # --- Strict processors ------------------------------------------------------
    def _process_account_orders(self, orders: list[LighterAccountOrderStrict]) -> None:
        """Process account order updates."""
        now = self._clock.timestamp_ns()
        self._dlog("process_orders:", "count=", len(orders))
        for od in orders:
            # Resolve instrument and client order ID
            inst_id = self._resolve_instrument_id(od.market_index)
            if inst_id is None:
                continue

            coid = self._resolve_client_order_id(od.order_index, od.client_order_index)
            if coid is None:
                # On startup or for external GUI orders there may be no local
                # ClientOrderId mapping yet. Instead of ignoring such orders we
                # synthesize a stable ClientOrderId and register a simple
                # mapping so they participate in reconciliation as external
                # orders (similar to other adapters).
                coid_str = f"LIGHTER-EXT-{od.market_index}-{od.order_index}"
                try:
                    coid = ClientOrderId(coid_str)
                except Exception:
                    # Fallback: if ClientOrderId construction fails, skip this
                    # entry rather than risking impact on normal orders.
                    self._dlog("process_orders_ext_coid_failed:", coid_str)
                    continue
                # Mapping is best-effort and only used for later correlation
                # (for example when resolving venue_order_index from trades).
                self._coid_by_venue_order_index[od.order_index] = coid
                self._dlog("process_orders_ext_coid:", od.order_index, coid.value)

            strat = self._cache.strategy_id_for_order(coid)
            if strat is None:
                # External order - generate OrderStatusReport (like Bybit)
                self._log.debug(f"No strategy found for order {coid}, generating OrderStatusReport")
                report = od.parse_to_order_status_report(
                    account_id=self.account_id,
                    instrument_id=inst_id,
                    client_order_id=coid,
                    venue_order_id=VenueOrderId(str(od.order_index)),
                    enum_parser=_ENUM,
                    ts_init=now,
                )
                self._send_order_status_report(report)
                continue

            # Get full order object from cache (like Bybit). If order not in cache,
            # we will still emit OrderStatusReport or best-effort events based on WS data.
            order = self._cache.order(coid)

            status_norm = _ENUM.parse_order_status(od.status)
            if status_norm is None:
                self._dlog("process_orders_unknown_status:", od.status)
                continue

            voi = VenueOrderId(str(od.order_index))

            self._log.debug(
                f"Processing order {coid}: status={od.status} -> {status_norm}, "
                f"already_accepted={coid.value in self._accepted_coids}",
            )

            if status_norm == OrderStatus.ACCEPTED:
                self._handle_order_accepted_transition(
                    strat=strat,
                    inst_id=inst_id,
                    coid=coid,
                    voi=voi,
                    order=order,
                    od=od,
                    now=now,
                )
            elif status_norm == OrderStatus.FILLED:
                # Ensure we at least emit OrderAccepted before a terminal FILL
                if coid.value not in self._accepted_coids:
                    self._register_venue_mapping(coid, voi)
                    self._accepted_coids.add(coid.value)
                    self.generate_order_accepted(
                        strategy_id=strat,
                        instrument_id=inst_id,
                        client_order_id=coid,
                        venue_order_id=voi,
                        ts_event=now,
                    )
            elif status_norm in (OrderStatus.CANCELED, OrderStatus.EXPIRED):
                if coid.value in self._canceled_coids:
                    # Already processed cancellation for this coid
                    continue
                self._canceled_coids.add(coid.value)
                # If we never saw an ACCEPTED frame (for example due to WS
                # loss), still ensure a venue mapping exists so downstream
                # components see a consistent order lifecycle.
                if coid.value not in self._accepted_coids:
                    try:
                        self._register_venue_mapping(coid, voi)
                        self._accepted_coids.add(coid.value)
                    except Exception:
                        pass
                self.generate_order_canceled(
                    strategy_id=strat,
                    instrument_id=inst_id,
                    client_order_id=coid,
                    venue_order_id=voi,
                    ts_event=now,
                )
            elif status_norm == OrderStatus.REJECTED:
                self.generate_order_rejected(
                    strategy_id=strat,
                    instrument_id=inst_id,
                    client_order_id=coid,
                    reason=od.status,
                    ts_event=now,
                )

    def _handle_order_accepted_transition(
        self,
        *,
        strat: StrategyId,
        inst_id: InstrumentId,
        coid: ClientOrderId,
        voi: VenueOrderId,
        order: Order | None,
        od: LighterAccountOrderStrict,
        now: int,
    ) -> None:
        """Handle transitions when venue status normalizes to ACCEPTED."""
        if coid.value not in self._accepted_coids:
            # First time we see this order as active → ACCEPTED
            self._register_venue_mapping(coid, voi)
            self._accepted_coids.add(coid.value)
            self.generate_order_accepted(
                strategy_id=strat,
                instrument_id=inst_id,
                client_order_id=coid,
                venue_order_id=voi,
                ts_event=now,
            )
            return

        # Already accepted: treat changes in price/qty as OrderUpdated, even
        # if local status is not explicitly PENDING_UPDATE. This improves
        # event completeness when some intermediate states are skipped.
        should_update = False
        new_qty: Quantity | None = None
        new_px: Price | None = None
        try:
            if od.remaining_base_amount is not None:
                new_qty = Quantity.from_str(od.remaining_base_amount)
            if od.price is not None:
                new_px = Price.from_str(od.price)
        except Exception:
            # best-effort only
            pass

        if order is not None:
            if new_px is not None and new_px != order.price:
                should_update = True
            if new_qty is not None and new_qty != order.quantity:
                should_update = True

        # Keep the explicit PENDING_UPDATE status as a strong signal.
        if order is not None and order.status == OrderStatus.PENDING_UPDATE:
            should_update = True

        if not should_update:
            self._dlog(
                "process_orders_no_changes:",
                coid.value,
                "status=",
                order.status if order else None,
            )
            return

        self.generate_order_updated(
            strategy_id=strat,
            instrument_id=inst_id,
            client_order_id=coid,
            venue_order_id=voi,
            quantity=new_qty or (order.quantity if order else None),
            price=new_px or (order.price if order else None),
            trigger_price=(
                Price.from_str(od.trigger_price)
                if od.trigger_price and od.trigger_price != "0"
                else None
            ),
            ts_event=now,
        )

    def _process_account_trades(self, trades: list[LighterAccountTradeStrict]) -> None:
        """Process account trade updates (fills only).

        Notes
        -----
        - This method is *only* responsible for fill-related artifacts:
          `FillReport` and `OrderFilled` events.
        - It MUST NOT update position state; positions are derived exclusively
          from HTTP / `account_all_positions` snapshots in
          :meth:`_process_account_positions`.
        """
        now = self._clock.timestamp_ns()
        for tr in trades:
            # Deduplicate
            if self._is_duplicate_trade(tr.trade_id):
                continue

            # Find our order (venue order id as string)
            our_order_id = self._find_our_order_id(tr)
            if our_order_id is None:
                # Trade does not belong to any locally-known order; ignore.
                continue

            vid = _norm_parse_int(our_order_id)
            if vid is None:
                continue

            coid = self._coid_by_venue_order_index.get(vid)
            if coid is None:
                # We do not have a ClientOrderId mapping for this venue order; ignore.
                continue

            # Resolve strategy from cache; only trades for known strategy orders
            # should generate OrderFilled/FillReport events.
            try:
                strat = self._cache.strategy_id_for_order(coid)
            except Exception:
                strat = None
            if strat is None:
                continue

            # Resolve instrument from either trade.market_id or cache order.
            inst_id = self._resolve_instrument_id(tr.market_id)
            if inst_id is None:
                try:
                    order_obj = self._cache.order(coid)
                except Exception:
                    order_obj = None
                if order_obj is not None:
                    inst_id = order_obj.instrument_id
            if inst_id is None:
                # Cannot resolve instrument; skip this trade to avoid inconsistent events.
                continue

            inst = self._ensure_instrument_cached(inst_id)
            if inst is None:
                continue

            # Determine maker/taker and calculate commission
            is_maker, order_side = self._determine_maker_taker(tr, our_order_id)
            commission = self._calculate_commission(tr, is_maker, inst.quote_currency)

            voi = VenueOrderId(our_order_id)
            lid = LiquiditySide.MAKER if is_maker else LiquiditySide.TAKER

            # Emit FillReport
            report = FillReport(
                account_id=self.account_id,
                instrument_id=inst_id,
                venue_order_id=voi,
                trade_id=TradeId(str(tr.trade_id)),
                order_side=order_side,
                last_qty=inst.make_qty(tr.size),
                last_px=inst.make_price(tr.price),
                commission=commission,
                liquidity_side=lid,
                report_id=UUID4(),
                ts_event=now,
                ts_init=now,
                client_order_id=coid,
            )
            self._send_fill_report(report)

            # Generate OrderFilled event
            order = self._cache.order(coid)
            order_type = order.order_type if order is not None else OrderType.LIMIT
            self.generate_order_filled(
                strategy_id=strat,
                instrument_id=inst_id,
                client_order_id=coid,
                venue_order_id=voi,
                venue_position_id=None,
                trade_id=TradeId(str(tr.trade_id)),
                order_side=order_side,
                order_type=order_type,
                last_qty=inst.make_qty(tr.size),
                last_px=inst.make_price(tr.price),
                quote_currency=inst.quote_currency,
                commission=commission,
                liquidity_side=lid,
                ts_event=now,
                info=None,
            )

    def _process_account_positions(self, positions: list[LighterAccountPositionStrict]) -> None:
        """Process account position snapshots.

        Notes
        -----
        - Positions are treated in NETTING mode: one signed net size per
          instrument (LONG/SHORT/FLAT).
        - This method emits `PositionStatusReport` only; it does *not* generate
          any `OrderFilled` / `FillReport` events.
        - De-duplication is value-based using the last seen signed size and
          average entry price per `market_id`.
        """
        now = self._clock.timestamp_ns()
        for p in positions:
            inst_id = self._market_instrument.get(p.market_id)
            if inst_id is None and isinstance(self._instrument_provider, LighterInstrumentProvider):
                inst_id = self._instrument_provider.find_by_market_index(p.market_id)
                if inst_id is not None:
                    self._market_instrument[p.market_id] = inst_id
            if inst_id is None:
                continue
            inst = self._cache.instrument(inst_id)
            if inst is None:
                inst = self._ensure_instrument_cached(inst_id)
                if inst is None:
                    continue
            signed_dec = Decimal(str(p.position))
            avg_px_open_dec = Decimal(str(p.avg_entry_price)) if p.avg_entry_price is not None else None
            # De-dup: skip if state unchanged (value-based). Track last_seen_ts for optional suppression.
            prev = self._last_position_state.get(p.market_id)
            if prev is not None:
                prev_size, prev_avg, prev_ts = prev
                if prev_size == signed_dec and prev_avg == avg_px_open_dec:
                    # Optional time-window suppression (in case venues burst-repeat identical frames)
                    if self._position_dedup_suppress_window_ns is None or (now - prev_ts) < self._position_dedup_suppress_window_ns:
                        continue
            self._last_position_state[p.market_id] = (signed_dec, avg_px_open_dec, now)

            if signed_dec == 0:
                report = PositionStatusReport.create_flat(
                    account_id=self.account_id,
                    instrument_id=inst_id,
                    size_precision=inst.size_precision,
                    ts_init=self._clock.timestamp_ns(),
                )
                self._send_position_status_report(report)
                continue
            side = PositionSide.LONG if signed_dec > 0 else PositionSide.SHORT
            qty = inst.make_qty(str(abs(signed_dec)))
            avg_px_open = avg_px_open_dec
            report = PositionStatusReport(
                account_id=self.account_id,
                instrument_id=inst_id,
                position_side=side,
                quantity=qty,
                report_id=UUID4(),
                ts_last=now,
                ts_init=self._clock.timestamp_ns(),
                # Netting mode: omit venue_position_id so reconciliation treats this as
                # a single NET position per instrument.
                venue_position_id=None,
                avg_px_open=avg_px_open,
            )
            self._send_position_status_report(report)

            # Update position cache for margin calculation
            self._positions[p.market_id] = {
                'position': str(p.position),
                'position_value': str(p.position_value) if p.position_value else "0",
                'initial_margin_fraction': str(p.initial_margin_fraction) if p.initial_margin_fraction else "0",
                'unrealized_pnl': str(p.unrealized_pnl) if p.unrealized_pnl else "0",
            }

    def _calculate_position_margin(self, market_index: int) -> Decimal:
        """Calculate position margin for a specific market.

        Uses cached position data from account_all_positions.
        """
        pos_data = self._positions.get(market_index)
        if not pos_data:
            return Decimal("0")

        position_value = Decimal(pos_data.get('position_value', '0'))
        margin_fraction = Decimal(pos_data.get('initial_margin_fraction', '0'))

        if position_value == 0 or margin_fraction == 0:
            return Decimal("0")

        return position_value * margin_fraction / Decimal("100")

    def _calculate_total_margin(self) -> Decimal:
        """Calculate total margin requirement (positions only).

        Note: Order margin calculation requires iterating open orders from cache,
        which is not implemented yet. For now, only position margin is calculated.

        TODO: Add order margin calculation including:
        - Open orders (excluding reduce_only)
        - Pending orders (conditional orders)
        """
        total_margin = Decimal("0")

        # Calculate position margin for all markets
        for market_index in self._positions.keys():
            position_margin = self._calculate_position_margin(market_index)
            total_margin += position_margin

        return total_margin

    # --- Periodic reconciliation interfaces -----------------------------------------
    async def generate_position_status_reports(
        self,
        command: GeneratePositionStatusReports,
    ) -> list[PositionStatusReport]:
        """
        Generate a snapshot of `PositionStatusReport` objects via HTTP.

        Notes
        -----
        - Uses `/api/v1/account` (get_account_state_strict) to obtain a full
          account snapshot, then maps `positions` to `PositionStatusReport`.
        - Does *not* emit reports on the message bus; the caller (engine) is
          responsible for consuming and reconciling them.
        """
        try:
            state = await self._http_with_auth(
                self._http_account.get_account_state_strict,
                account_index=self._creds.account_index,
            )
        except Exception as exc:
            self._log.exception("generate_position_status_reports HTTP error", exc)
            return []

        positions = state.positions or []
        if not positions:
            return []

        # Optional filter by instrument_id
        target_market_ids: set[int] | None = None
        if command.instrument_id is not None:
            mid = self._get_market_index(command.instrument_id.value)
            if mid is None:
                return []
            target_market_ids = {mid}

        now = self._clock.timestamp_ns()
        reports: list[PositionStatusReport] = []

        for p in positions:
            if target_market_ids is not None and p.market_id not in target_market_ids:
                continue

            inst_id = self._resolve_instrument_id(p.market_id)
            if inst_id is None:
                continue

            inst = self._ensure_instrument_cached(inst_id)
            if inst is None:
                continue

            signed_dec = Decimal(str(p.position))
            if signed_dec == 0:
                report = PositionStatusReport.create_flat(
                    account_id=self.account_id,
                    instrument_id=inst_id,
                    size_precision=inst.size_precision,
                    ts_init=now,
                )
            else:
                side = PositionSide.LONG if signed_dec > 0 else PositionSide.SHORT
                qty = inst.make_qty(str(abs(signed_dec)))
                avg_px_open = (
                    Decimal(str(p.avg_entry_price)) if p.avg_entry_price is not None else None
                )
                report = PositionStatusReport(
                    account_id=self.account_id,
                    instrument_id=inst_id,
                    position_side=side,
                    quantity=qty,
                    report_id=UUID4(),
                    ts_last=now,
                    ts_init=now,
                    venue_position_id=None,
                    avg_px_open=avg_px_open,
                )

            reports.append(report)

        return reports

    async def generate_order_status_reports(
        self,
        command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        """
        Generate a list of `OrderStatusReport` objects via HTTP active orders.

        Notes
        -----
        - Uses `/api/v1/accountActiveOrders` per `market_index` to obtain the
          current set of active orders for each relevant market.
        - Only markets which are relevant for this client (open orders or
          open positions in cache, or explicitly requested instrument) are
          queried, to avoid unnecessary API usage.
        - External orders are represented with synthesized `ClientOrderId`
          values of the form `LIGHTER-EXT-<market>-<order_index>`.
        """
        # Determine markets to query
        market_indices: set[int] = set()

        if command.instrument_id is not None:
            mid = self._get_market_index(command.instrument_id.value)
            if mid is None:
                return []
            market_indices.add(mid)
        else:
            # Use cache to discover markets with local activity (orders or positions)
            try:
                open_orders = self._cache.orders_open(venue=self.venue)
            except Exception:
                open_orders = []
            try:
                open_positions = self._cache.positions_open(venue=self.venue)
            except Exception:
                open_positions = []

            for o in open_orders:
                mid = self._get_market_index(str(o.instrument_id))
                if mid is not None:
                    market_indices.add(mid)

            for p in open_positions:
                mid = self._get_market_index(str(p.instrument_id))
                if mid is not None:
                    market_indices.add(mid)

        if not market_indices:
            return []

        all_orders: list[LighterAccountOrderStrict] = []
        for mid in market_indices:
            try:
                actives = await self._http_with_auth(
                    self._http_account.get_account_active_orders,
                    account_index=self._creds.account_index,
                    market_index=mid,
                )
            except LighterHttpError as exc:
                self._log.debug(f"generate_order_status_reports active_orders error: {exc}")
                continue
            except Exception as exc:
                self._log.exception("generate_order_status_reports HTTP error", exc)
                continue
            if actives:
                all_orders.extend(actives)

        if not all_orders:
            return []

        now = self._clock.timestamp_ns()
        reports: list[OrderStatusReport] = []

        for od in all_orders:
            inst_id = self._resolve_instrument_id(od.market_index)
            if inst_id is None:
                continue

            coid = self._resolve_client_order_id(od.order_index, od.client_order_index)
            if coid is None:
                # External order relative to this client; synthesize a ClientOrderId.
                coid = ClientOrderId(f"LIGHTER-EXT-{od.market_index}-{od.order_index}")
                self._coid_by_venue_order_index[od.order_index] = coid

            try:
                report = od.parse_to_order_status_report(
                    account_id=self.account_id,
                    instrument_id=inst_id,
                    client_order_id=coid,
                    venue_order_id=VenueOrderId(str(od.order_index)),
                    enum_parser=_ENUM,
                    ts_init=now,
                )
            except Exception as exc:
                self._log.debug(f"generate_order_status_reports parse error: {exc}")
                continue

            reports.append(report)

        # Optional: log receipt similar to other adapters
        count = len(reports)
        if count:
            plural = "" if count == 1 else "s"
            msg = f"generate_order_status_reports produced {count} OrderStatusReport{plural}"
            self._log.debug(msg)

        return reports

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        """
        Generate a single `OrderStatusReport` for the given order (best-effort).

        Notes
        -----
        - This method is primarily used for in-flight checks and manual
          QueryOrder requests.
        - It derives the relevant `market_index` using the strongest hint:
          cache order → venue mapping → instrument_id, then queries
          `/api/v1/accountActiveOrders` for that market.
        """
        client_order_id = command.client_order_id
        venue_order_id = command.venue_order_id

        # Derive market_index from cache/mappings/instrument_id
        market_index: int | None = None

        # 1) Try from client_order_id via cache
        if client_order_id is not None:
            order = self._cache.order(client_order_id)
            if order is not None:
                market_index = self._get_market_index(str(order.instrument_id))

        # 2) Try from venue_order_id via mapping
        if market_index is None and venue_order_id is not None:
            try:
                oid_val = int(venue_order_id.value)
            except Exception:
                oid_val = None
            if oid_val is not None:
                coid = self._coid_by_venue_order_index.get(oid_val)
                if coid is not None:
                    order = self._cache.order(coid)
                    if order is not None:
                        market_index = self._get_market_index(str(order.instrument_id))

        # 3) Fallback: use instrument_id from command
        if market_index is None and command.instrument_id is not None:
            market_index = self._get_market_index(command.instrument_id.value)

        if market_index is None:
            self._log.debug(
                "generate_order_status_report: could not determine market_index "
                f"for client_order_id={client_order_id} venue_order_id={venue_order_id}",
            )
            return None

        # Query active orders for this market
        try:
            actives = await self._http_with_auth(
                self._http_account.get_account_active_orders,
                account_index=self._creds.account_index,
                market_index=market_index,
            )
        except LighterHttpError as exc:
            self._log.debug(f"generate_order_status_report active_orders error: {exc}")
            return None
        except Exception as exc:
            self._log.exception("generate_order_status_report HTTP error", exc)
            return None

        if not actives:
            return None

        # Find matching order
        target: LighterAccountOrderStrict | None = None
        # Prefer matching by venue_order_id if available
        if venue_order_id is not None:
            try:
                oid_val = int(venue_order_id.value)
            except Exception:
                oid_val = None
            if oid_val is not None:
                for od in actives:
                    if od.order_index == oid_val:
                        target = od
                        break
        # Fallback: try match by client_order_index mapping
        if target is None and client_order_id is not None:
            coi = self._client_order_index_by_coid.get(client_order_id.value)
            if coi is not None:
                for od in actives:
                    if od.client_order_index == coi:
                        target = od
                        break

        if target is None:
            return None

        inst_id = self._resolve_instrument_id(target.market_index)
        if inst_id is None:
            return None

        coid = client_order_id
        if coid is None:
            coid = self._resolve_client_order_id(target.order_index, target.client_order_index)
            if coid is None:
                coid = ClientOrderId(f"LIGHTER-EXT-{target.market_index}-{target.order_index}")

        now = self._clock.timestamp_ns()
        try:
            report = target.parse_to_order_status_report(
                account_id=self.account_id,
                instrument_id=inst_id,
                client_order_id=coid,
                venue_order_id=VenueOrderId(str(target.order_index)),
                enum_parser=_ENUM,
                ts_init=now,
            )
        except Exception as exc:
            self._log.debug(f"generate_order_status_report parse error: {exc}")
            return None

        return report

    async def generate_fill_reports(
        self,
        command: GenerateFillReports,
    ) -> list[FillReport]:
        """
        Generate a list of `FillReport` objects (not implemented for Lighter).

        Notes
        -----
        - Lighter currently relies on private WS `account_all_trades` for fill
          events. HTTP-based historical fills are not yet implemented.
        - This method returns an empty list to satisfy the abstract interface.
        """
        self._log.debug("generate_fill_reports not implemented for Lighter; returning empty list")
        return []

    async def _subscribe_private_channels(self) -> None:
        if not self._ws:
            return
        # public keepalive
        await self._ws.subscribe("market_stats/all")
        # private
        token = self._auth.token()
        self._dlog("auth_token_len:", len(token) if token else 0)
        if token and token != "dummy-auth-token":
            account_idx = self._creds.account_index
            if self._subscribe_account_stats:
                self._dlog("subscribe:", f"account_stats/{account_idx}")
                await self._ws.send({"type": "subscribe", "channel": f"account_stats/{account_idx}", "auth": token})
            # Always subscribe to split channels (required for order updates)
            self._dlog("subscribe:", f"account_all_orders/{account_idx}")
            await self._ws.send({"type": "subscribe", "channel": f"account_all_orders/{account_idx}", "auth": token})
            self._dlog("subscribe:", f"account_all_trades/{account_idx}")
            await self._ws.send({"type": "subscribe", "channel": f"account_all_trades/{account_idx}", "auth": token})
            self._dlog("subscribe:", f"account_all_positions/{account_idx}")
            await self._ws.send({"type": "subscribe", "channel": f"account_all_positions/{account_idx}", "auth": token})
            parts = ["account_all_orders", "account_all_trades", "account_all_positions"]
            if self._subscribe_account_stats:
                parts.append("account_stats")
            self._log.info(f"Subscribed private channels: {', '.join(parts)}", LogColor.BLUE)

    # --- helper: instrument & scaling -----------------------------------------------------------
    def _get_market_index(self, instrument_id_str: str) -> int | None:
        """Return the Lighter market_index for a given InstrumentId string.

        The mapping is populated from the instrument provider on connect and
        refreshed lazily via `_resolve_instrument_id` when new markets are
        discovered from private streams.
        """
        for mid, iid in self._market_instrument.items():
            if str(iid) == instrument_id_str:
                return mid
        return None

    # Removed legacy _scale_price_size in favor of common.normalize helpers used in-place

    def _prepare_order(self, order: Order) -> _PreparedCreateOrder | None:
        inst_id = order.instrument_id
        market_index = self._get_market_index(str(inst_id))
        if market_index is None:
            self._log.error(f"No market_id for {inst_id}")
            return None

        try:
            order_type = _ENUM.parse_nautilus_order_type(order.order_type)
        except Exception:
            self._log.error(f"Unsupported order type {order.order_type}")
            return None

        side = order.side
        is_ask = side == OrderSide.SELL

        inst = self._cache.instrument(inst_id)
        if inst is None:
            # Try provider, and publish to DataEngine to populate cache
            inst = self._instrument_provider.get_all().get(inst_id)
            if inst is not None:
                try:
                    self._msgbus.send(endpoint="DataEngine.process", msg=inst)
                except Exception:
                    pass
        if inst is None:
            self._log.error(f"Instrument {inst_id} not available in cache or provider")
            self._dlog("prepare_no_instrument:", str(inst_id))
            return None
        price_p = inst.price_precision
        size_p = inst.size_precision

        # Convert Price/Quantity to Decimal
        if isinstance(order.price, Price):
            price_decimal = order.price.as_decimal()
        else:
            price_decimal = order.price
        if isinstance(order.quantity, Quantity):
            qty_decimal = order.quantity.as_decimal()
        else:
            qty_decimal = order.quantity

        price_i, norm_price_dec = _norm_normalize_price(
            price_decimal,
            price_decimals=price_p,
            price_increment=inst.price_increment,
        )
        size_i, norm_size_dec = _norm_normalize_size(
            qty_decimal,
            size_decimals=size_p,
            size_increment=inst.size_increment,
        )

        if norm_price_dec is not None and norm_size_dec is not None:
            min_notional_dec = _norm_money_to_decimal(inst.min_notional)
            if min_notional_dec is not None and (norm_price_dec * norm_size_dec) < min_notional_dec:
                self._log.error("Order notional below min_notional; adjust price/size")
                self._dlog("prepare_below_min_notional:", norm_price_dec, norm_size_dec, min_notional_dec)
                return None
        if size_i is None or size_i <= 0:
            self._log.error("Missing/invalid quantity for order")
            self._dlog(
                "prepare_invalid_size:",
                str(order.quantity),
                "size_decimals=",
                size_p,
                "size_increment=",
                inst.size_increment,
            )
            return None

        price_payload = price_i
        if price_payload is None:
            if _ENUM.requires_price(order_type):
                self._log.error(f"Missing price for {order.order_type.name} order")
                self._dlog("prepare_missing_price:", order.order_type)
                return None
            price_payload = self._derive_market_price(inst, inst_id, price_p, is_ask)
            if price_payload is None:
                self._log.error("Unable to derive price for MARKET order; supply acceptable execution price or ensure order book is available")
                self._dlog("prepare_derive_price_failed")
                return None

        # Trigger price: only read/scale for order types which require it;
        # for all other types use 0.
        trigger_i = 0
        if _ENUM.requires_trigger_price(order_type):
            trig_raw = order.trigger_price
            trigger_dec = _norm_to_decimal(trig_raw)
            if trigger_dec is None:
                self._log.error(f"Missing trigger_price for {order.order_type.name} order")
                return None
            trig_int, _ = _norm_normalize_price(
                trigger_dec,
                price_decimals=price_p,
                price_increment=inst.price_increment,
            )
            trigger_i = trig_int or 0

        reduce_only = bool(order.is_reduce_only)

        tif_val = _ENUM.parse_nautilus_time_in_force(order.time_in_force, bool(order.is_post_only))

        order_expiry = 0 if tif_val == LighterTimeInForce.IOC else -1

        coid_value = order.client_order_id.value
        digest = hashlib.blake2b(coid_value.encode("utf-8"), digest_size=8).digest()
        # Venue constraint: ClientOrderIndex must be in [1, 2^48-1].
        raw_idx = int.from_bytes(digest, byteorder="big", signed=False)
        client_order_index = raw_idx & 0x0000_FFFF_FFFF_FFFF
        if client_order_index <= 0:
            client_order_index = 1

        self._dlog("prepare_success:", coid_value, "coi=", client_order_index)
        return _PreparedCreateOrder(
            order=order,
            instrument_id=inst_id,
            strategy_id=order.strategy_id,
            coid_value=coid_value,
            market_index=market_index,
            client_order_index=client_order_index,
            base_amount=size_i,
            price=price_payload,
            trigger_price=trigger_i,
            order_type=order_type,
            time_in_force=tif_val,
            reduce_only=reduce_only,
            is_ask=is_ask,
            order_expiry=order_expiry,
        )

    # --- Core command handlers ---------------------------------------------------
    async def _submit_order(self, command: SubmitOrder) -> None:
        prepared = self._prepare_order(command.order)
        if prepared is None:
            return
        order = prepared.order
        # Register local mappings for reconciliation.
        coid_value = prepared.coid_value
        self._client_order_index_by_coid[coid_value] = prepared.client_order_index
        self._coid_by_client_order_index[prepared.client_order_index] = order.client_order_id
        # Mark last tx action kind for this client_order_index.
        self._last_tx_action_by_client_index[prepared.client_order_index] = "create"
        # Emit submitted event prior to transport (consistent event sequencing).
        self.generate_order_submitted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )
        # Transport handles signing / nonce / WS-HTTP routing.
        try:
            await self._transport.submit_order(prepared)
        except Exception as exc:
            self._log.error(f"Order submit transport error: {exc}")
            return
        # Optional: schedule a one-shot HTTP reconcile after a short delay.
        if self._post_submit_http_reconcile:
            delay = max(0.0, float(self._post_submit_poll_interval_ms) / 1000.0)
            self.create_task(
                self.run_after_delay(
                    delay,
                    self._http_reconcile_order_index(prepared.market_index, prepared.client_order_index),
                ),
                log_msg="reconcile_order_index",
            )
        self._dlog("submitted:", str(order.client_order_id), "coi=", prepared.client_order_index, "mid=", prepared.market_index)

    async def _submit_order_list(self, command: SubmitOrderList) -> None:
        """Submit a batch of orders as a single Lighter `sendtxbatch` transaction.

        Notes
        -----
        - `SubmitOrderList` wraps an `OrderList` (`command.order_list.orders`).
        - We still emit `OrderSubmitted` per order (for consistent event sequencing),
          but the actual transport uses a single JSONAPI `sendtxbatch` call, which
          is more efficient than looping one-by-one.
        """
        prepared_orders: list[_PreparedCreateOrder] = []
        order_list = command.order_list

        for order in order_list.orders:
            po = self._prepare_order(order)
            if po is None:
                continue
            # Register local mappings and emit Submitted per order.
            coid_value = po.coid_value
            self._client_order_index_by_coid[coid_value] = po.client_order_index
            self._coid_by_client_order_index[po.client_order_index] = order.client_order_id
            self._last_tx_action_by_client_index[po.client_order_index] = "create"
            self.generate_order_submitted(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                ts_event=self._clock.timestamp_ns(),
            )
            prepared_orders.append(po)
        if not prepared_orders:
            return
        try:
            await self._transport.submit_order_list(prepared_orders)
        except Exception as exc:
            self._log.error(f"Batch submit transport error: {exc}")

    async def _cancel_order(self, command: CancelOrder) -> None:
        market_index = self._get_market_index(str(command.instrument_id))
        if market_index is None:
            self._log.warning(f"CancelOrder: no market_id for {command.instrument_id}; skipping")
            return
        if not command.venue_order_id:
            self._log.warning("CancelOrder missing venue_order_id; skipping")
            return
        order_index = _norm_parse_int(command.venue_order_id.value)
        if order_index is None:
            self._log.warning(
                "CancelOrder: unable to parse venue_order_id as integer; skipping "
                f"value={command.venue_order_id.value!r}",
            )
            return
        try:
            await self._transport.cancel_order(market_index=market_index, order_index=order_index)
        except Exception as exc:
            self._log.error(f"Cancel transport error: {exc}")

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        try:
            await self._transport.cancel_all()
        except Exception as exc:
            self._log.error(f"Cancel-all transport error: {exc}")

    # --- Post-submit HTTP reconciliation (optional) ----------------------------------
    async def _http_reconcile_order_index(self, market_index: int, client_order_index: int) -> None:
        """One-shot HTTP reconcile to backfill client→venue order mapping.

        Simpler strategy: query open orders once for the account/market and map
        the first match on `client_order_index`. No transaction fallback, no loop.
        Intended as a rare fallback when private WS mapping is delayed.
        """
        try:
            acct = self._creds.account_index
            orders = await self._http_with_auth(
                self._http_account.get_account_orders,
                account_index=acct,
                market_index=market_index,
            )
            if not orders:
                return
            for od in orders:
                if od.client_order_index == client_order_index:
                    coid = self._coid_by_client_order_index.get(client_order_index)
                    if coid is None:
                        return
                    self._register_venue_mapping(coid, VenueOrderId(str(od.order_index)))
                    self._log.info(
                        f"HTTP reconcile mapped coi={client_order_index} -> oi={od.order_index}",
                    )
                    self._dlog("http_reconcile:", client_order_index, od.order_index)
                    return
        except Exception as exc:
            # Best-effort helper; HTTP failures here should not affect core flow.
            self._log.debug(f"http_reconcile_order_index error: {exc}")

    async def change_api_key(self, eth_private_key: str, new_pubkey_hex: str, nonce: int | None = None) -> tuple[dict | None, str | None]:
        """Thin wrapper to change the API key public key for this account.

        Calls through to the signer, which generates the tx_info, performs the
        L1 signature, and submits the transaction via REST. Returns the REST
        response dict and an error string if any.
        """
        return await self._signer.change_api_key(
            eth_private_key=eth_private_key,
            new_pubkey_hex=new_pubkey_hex,
            nonce=nonce,
        )

    async def _batch_cancel_orders(self, command: BatchCancelOrders) -> None:
        items: list[tuple[int, int]] = []
        for cancel in command.cancels:
            market_index = self._get_market_index(str(cancel.instrument_id))
            if market_index is None:
                self._log.debug(f"BatchCancelOrders: no market_id for {cancel.instrument_id}; skipping item")
                continue
            if not cancel.venue_order_id:
                continue
            order_index = _norm_parse_int(cancel.venue_order_id.value)
            if order_index is None:
                continue
            items.append((market_index, order_index))
        if not items:
            return
        try:
            await self._transport.cancel_orders(items)
        except Exception as exc:
            self._log.error(f"Batch cancel transport error: {exc}")

    async def _modify_order(self, command: ModifyOrder) -> None:
        """Modify an existing order on Lighter via go-FFI signer."""
        inst_id = command.instrument_id
        market_index = self._get_market_index(str(inst_id))
        if market_index is None:
            self._log.error(f"No market_id for {inst_id}")
            return
        # Resolve order_index
        order_index: int | None = None
        if command.venue_order_id is not None:
            order_index = _norm_parse_int(command.venue_order_id.value)
        if order_index is None:
            coi_val = command.client_order_id.value
            mapped = self._venue_order_id_by_coid.get(coi_val)
            if mapped is not None:
                order_index = _norm_parse_int(mapped.value)
        if order_index is None:
            self._log.error("ModifyOrder requires venue_order_id or known mapping for client_order_id")
            return
        # Track last tx action for this order (by order_index and client_order_index if available)
        self._last_tx_action_by_order_index[order_index] = "modify"
        coi_for_coid = self._client_order_index_by_coid.get(command.client_order_id.value)
        if coi_for_coid is not None:
            self._last_tx_action_by_client_index[coi_for_coid] = "modify"
        # Scale fields; align with lighter-python behavior by always sending explicit
        # base_amount and price (using existing order values as fallback).
        inst = self._cache.instrument(inst_id)
        if inst is None:
            inst = self._instrument_provider.find(inst_id)
        if inst is None:
            self._log.error(f"Instrument {inst_id} not available in cache or provider for modify")
            return
        price_p = inst.price_precision
        size_p = inst.size_precision

        # Fetch existing order; required to derive fallback price/size
        existing_order = self._cache.order(command.client_order_id)
        if existing_order is None:
            self._log.error(f"Existing order {command.client_order_id} not found in cache for modify")
            return

        # Derive target price/qty: command override existing values
        target_price_obj = command.price if command.price is not None else existing_order.price
        target_qty_obj = command.quantity if command.quantity is not None else existing_order.quantity

        # Normalize and scale
        price_decimal = target_price_obj.as_decimal()
        qty_decimal = target_qty_obj.as_decimal()

        price_i, _ = _norm_normalize_price(
            price_decimal,
            price_decimals=price_p,
            price_increment=inst.price_increment,
        )
        size_i, _ = _norm_normalize_size(
            qty_decimal,
            size_decimals=size_p,
            size_increment=inst.size_increment,
        )

        if price_i is None or size_i is None or size_i <= 0:
            self._log.error(f"Invalid modify target price/size: price_i={price_i}, size_i={size_i}")
            return

        base_amount = size_i
        price_scaled = price_i
        # Trigger price: align with lighter-python NIL_TRIGGER_PRICE semantics.
        # Official SDK uses NIL_TRIGGER_PRICE = 0 for non-conditional orders.
        if command.trigger_price is not None:
            trig_i, _ = _norm_normalize_price(
                command.trigger_price,
                price_decimals=price_p,
                price_increment=inst.price_increment,
            )
            trigger_scaled = trig_i if trig_i is not None else 0
        else:
            trigger_scaled = 0

        # Pass -1 directly to transport (Go signer converts -1 to uint32 max as sentinel)
        try:
            await self._transport.modify_order(
                market_index=market_index,
                order_index=order_index,
                base_amount=base_amount,
                price=price_scaled,
                trigger_price=trigger_scaled,
            )
        except Exception as exc:
            self._log.error(f"Modify transport error: {exc}")
            self.generate_order_modify_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.client_order_id,
                venue_order_id=command.venue_order_id,
                reason=str(exc),
                ts_event=self._clock.timestamp_ns(),
            )

    async def update_leverage(self, instrument_id: InstrumentId, leverage: float, margin_mode: int | str = 0) -> None:
        """Update leverage for a market (utility method).

        margin_mode: 0 (cross) or 1 (isolated), or 'cross'/'isolated'.
        leverage: e.g. 5.0 means 5x; fraction_bps becomes int(10000/leverage).
        """
        if not self._signer.available():
            self._log.warning("Lighter signer unavailable; skipping update_leverage")
            return
        market_index = self._get_market_index(str(instrument_id))
        if market_index is None:
            self._log.error(f"No market_id for {instrument_id}")
            return
        if isinstance(margin_mode, str):
            mm = margin_mode.strip().lower()
            margin_mode_i = 0 if mm in ("cross", "x", "c") else 1
        else:
            margin_mode_i = int(margin_mode)
        lev = Decimal(str(leverage))
        lev = Decimal("1") if lev <= 0 else lev
        fraction_bps = max(1, min(10000, int((Decimal("10000") / lev).quantize(Decimal(1), rounding=ROUND_HALF_UP))))
        try:
            await self._transport.update_leverage(
                market_index=market_index,
                fraction_bps=fraction_bps,
                margin_mode=margin_mode_i,
            )
        except Exception as exc:
            self._log.error(f"Update leverage transport error: {exc}")

    # --- Unified HTTP auth + retry helper ----------------------------------------------
    async def _http_with_auth(self, fn: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
        """Call a private HTTP endpoint with auth and one automatic refresh retry.

        Refreshes the token and retries once if the server returns 401 or code=20013.
        """
        # Use a 10-minute token horizon for long-running strategies; the manager
        # refreshes automatically 60 seconds before expiry.
        token = self._auth.token(horizon_secs=600)
        try:
            return await fn(**kwargs, auth=token)
        except LighterHttpError as exc:
            status = exc.status
            msg = exc.message if isinstance(exc.message, dict) else {}
            code = msg.get("code") if isinstance(msg, dict) else None
            if status == 401 or code == 20013:
                token = self._auth.token(horizon_secs=600, force=True)
                return await fn(**kwargs, auth=token)
            raise

    async def _fetch_and_emit_account_state_http(self) -> None:
        """Fetch account state snapshot via HTTP and emit as AccountState + positions."""
        # Use strict typed state to avoid dict parsing and keep naming consistent.
        state = await self._http_with_auth(
            self._http_account.get_account_state_strict,
            account_index=self._creds.account_index,
        )
        cur = self._quote_currency
        ts = self._clock.timestamp_ns()
        total_amt = Decimal(str(state.total_balance))
        free_amt = Decimal(str(state.available_balance))
        locked_amt = max(total_amt - free_amt, Decimal("0"))
        balances = [
            AccountBalance(
                total=Money(total_amt, cur),
                locked=Money(locked_amt, cur),
                free=Money(free_amt, cur),
            ),
        ]
        self.generate_account_state(
            balances=balances,
            margins=[],
            reported=True,
            ts_event=ts,
            info=None,
        )
        if state.positions:
            self._process_account_positions(state.positions)

    async def _fetch_and_emit_account_orders_http(self) -> None:
        """Best-effort HTTP snapshot for open orders (fallback to WS only if unsupported).

        On mainnet `/api/v1/accountOrders` may be restricted. In that case we
        fall back to `/api/v1/accountActiveOrders` per `market_index` to obtain
        a snapshot of currently active orders and feed them through the same
        strict order processor.
        """
        orders: list[LighterAccountOrderStrict] = []
        try:
            orders = await self._http_with_auth(
                self._http_account.get_account_orders,
                account_index=self._creds.account_index,
            )
        except LighterHttpError as exc:
            # Some deployments may not expose this endpoint; treat as optional.
            self._log.debug(f"HTTP get_account_orders not available: {exc}")
            orders = []

        # Fallback: use accountActiveOrders per known market_index when
        # accountOrders is unavailable or returns empty.
        if not orders:
            active_all: list[LighterAccountOrderStrict] = []
            # Only attempt fallback if we have a market mapping.
            if self._market_instrument:
                try:
                    for mid in self._market_instrument.keys():
                        actives = await self._http_with_auth(
                            self._http_account.get_account_active_orders,
                            account_index=self._creds.account_index,
                            market_index=mid,
                        )
                        if actives:
                            active_all.extend(actives)
                except LighterHttpError as exc:
                    self._log.debug(f"HTTP get_account_active_orders not available: {exc}")
            orders = active_all

        if not orders:
            return

        # Expect strict typed orders only; ignore otherwise.
        if isinstance(orders, list) and isinstance(orders[0], LighterAccountOrderStrict):
            self._process_account_orders(orders)
