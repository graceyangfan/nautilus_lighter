from __future__ import annotations

# -------------------------------------------------------------------------------------------------
#  Lighter transport service: signing + nonce + WS/HTTP send orchestration.
#  Keeps WS/HTTP clients transport-only. Execution remains thin (prepare → call → emit events).
# -------------------------------------------------------------------------------------------------

from typing import TYPE_CHECKING, Any, Callable, Protocol

from nautilus_trader.adapters.lighter.common.auth import LighterAuthManager
from nautilus_trader.adapters.lighter.common.enums import LighterCancelAllTif
from nautilus_trader.adapters.lighter.common.nonce import NonceManager
from nautilus_trader.adapters.lighter.common.signer import LighterSigner
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.adapters.lighter.http.transaction import LighterTransactionHttpClient
from nautilus_trader.adapters.lighter.websocket.client import LighterWebSocketClient
from nautilus_trader.common.component import LiveClock, Logger
from nautilus_trader.common.enums import LogColor


class _PreparedCreateOrderLike(Protocol):
    market_index: int
    client_order_index: int
    base_amount: int
    price: int
    is_ask: bool
    order_type: Any
    time_in_force: Any
    reduce_only: bool
    trigger_price: int
    order_expiry: int


if TYPE_CHECKING:  # only for type checkers; avoids runtime circular imports
    from nautilus_trader.adapters.lighter.execution import _PreparedCreateOrder

    PreparedCreateLike = _PreparedCreateOrder | _PreparedCreateOrderLike  # type: ignore[name-defined]
else:
    PreparedCreateLike = _PreparedCreateOrderLike


class LighterTransportService:
    """High-level transport orchestrator for Lighter private actions.

    Responsibilities
    - Acquire nonce (HTTP-backed) and sign via LighterSigner
    - Prefer WS `jsonapi/sendtx(_batch)`; optionally fallback to HTTP `sendTx(_Batch)`
    - Attach short-lived auth token
    - Generate stable request IDs

    Notes
    - No event emission or mapping inside this service.
    - WebSocket client is provided via a getter to avoid lifecycle ordering issues.
    """

    def __init__(
        self,
        *,
        clock: LiveClock,
        signer: LighterSigner,
        nonce: NonceManager,
        auth: LighterAuthManager,
        ws_getter: Callable[[], LighterWebSocketClient | None],
        http_tx: LighterTransactionHttpClient,
        http_account: LighterAccountHttpClient,
        http_send_fallback: bool = False,
    ) -> None:
        self._log = Logger(type(self).__name__)
        self._clock = clock
        self._signer = signer
        self._nonce = nonce
        self._auth = auth
        self._get_ws = ws_getter
        self._http_tx = http_tx
        self._http_account = http_account
        self._http_send_fallback = bool(http_send_fallback)

    # --- Nonce helpers -----------------------------------------------------------------
    async def _fetch_next_nonce(self, api_key_index: int) -> int:
        """Fetch the next nonce for the given API key from the venue."""
        # api_key_index is ignored; signer carries the effective account/api key.
        return await self._http_account.get_next_nonce(
            account_index=self._signer.account_index,
            api_key_index=self._signer.api_key_index,
        )

    async def _next_nonce(self) -> int:
        return await self._nonce.next(self._signer.api_key_index, self._fetch_next_nonce)

    async def refresh_nonce(self) -> int:
        """Force refresh the local nonce from the venue (on nonce error)."""
        return await self._nonce.rollback_and_refresh(self._signer.api_key_index, self._fetch_next_nonce)

    # --- Create ------------------------------------------------------------------------
    async def submit_order(self, po: PreparedCreateLike) -> None:
        if not self._signer.available():
            raise RuntimeError("Lighter signer unavailable; cannot submit order")
        nonce_val = await self._next_nonce()
        tx_info, err = await self._signer.sign_create_order_tx(
            market_index=po.market_index,
            client_order_index=po.client_order_index,
            base_amount=po.base_amount,
            price=po.price,
            is_ask=po.is_ask,
            order_type=po.order_type,
            time_in_force=po.time_in_force,
            reduce_only=po.reduce_only,
            trigger_price=po.trigger_price,
            order_expiry=po.order_expiry,
            nonce=nonce_val,
        )
        if err or not tx_info:
            raise RuntimeError(f"sign_create_order failed: {err}")

        tx_type = self._signer.get_tx_type_create_order()
        await self._send_ws_or_http(tx_type=tx_type, tx_info=tx_info)

    async def submit_order_list(self, pos: list[PreparedCreateLike]) -> None:
        if not self._signer.available():
            raise RuntimeError("Lighter signer unavailable; cannot submit orders")
        if not pos:
            return
        tx_types: list[int] = []
        tx_infos: list[str] = []
        tx_type_create = self._signer.get_tx_type_create_order()
        for po in pos:
            nonce_val = await self._next_nonce()
            tx_info, err = await self._signer.sign_create_order_tx(
                market_index=po.market_index,
                client_order_index=po.client_order_index,
                base_amount=po.base_amount,
                price=po.price,
                is_ask=po.is_ask,
                order_type=po.order_type,
                time_in_force=po.time_in_force,
                reduce_only=po.reduce_only,
                trigger_price=po.trigger_price,
                order_expiry=po.order_expiry,
                nonce=nonce_val,
            )
            if err or not tx_info:
                raise RuntimeError(f"sign_create_order (batch) failed: {err}")
            tx_types.append(tx_type_create)
            tx_infos.append(tx_info)
        await self._send_ws_or_http_batch(tx_types=tx_types, tx_infos=tx_infos)

    # --- Cancel / Modify / Leverage ----------------------------------------------------
    async def cancel_order(self, market_index: int, order_index: int) -> None:
        if not self._signer.available():
            raise RuntimeError("Lighter signer unavailable; cannot cancel order")
        nonce_val = await self._next_nonce()
        tx_info, err = await self._signer.sign_cancel_order_tx(
            market_index=market_index,
            order_index=order_index,
            nonce=nonce_val,
        )
        if err or not tx_info:
            raise RuntimeError(f"sign_cancel_order failed: {err}")
        await self._send_ws_or_http(tx_type=self._signer.get_tx_type_cancel_order(), tx_info=tx_info)

    async def cancel_all(self) -> None:
        if not self._signer.available():
            raise RuntimeError("Lighter signer unavailable; cannot cancel all")
        nonce_val = await self._next_nonce()
        tx_info, err = await self._signer.sign_cancel_all_orders_tx(
            time_in_force=LighterCancelAllTif.IMMEDIATE,
            time=0,
            nonce=nonce_val,
        )
        if err or not tx_info:
            raise RuntimeError(f"sign_cancel_all failed: {err}")
        await self._send_ws_or_http(tx_type=self._signer.get_tx_type_cancel_all(), tx_info=tx_info)

    async def cancel_orders(self, items: list[tuple[int, int]]) -> None:
        if not self._signer.available():
            raise RuntimeError("Lighter signer unavailable; cannot batch cancel")
        if not items:
            return
        tx_types: list[int] = []
        tx_infos: list[str] = []
        for market_index, order_index in items:
            nonce_val = await self._next_nonce()
            tx_info, err = await self._signer.sign_cancel_order_tx(
                market_index=market_index,
                order_index=order_index,
                nonce=nonce_val,
            )
            if err or not tx_info:
                raise RuntimeError(f"sign_cancel_order (batch) failed: {err}")
            tx_types.append(self._signer.get_tx_type_cancel_order())
            tx_infos.append(tx_info)
        await self._send_ws_or_http_batch(tx_types=tx_types, tx_infos=tx_infos)

    async def modify_order(
        self,
        market_index: int,
        order_index: int,
        *,
        base_amount: int,
        price: int,
        trigger_price: int,
    ) -> None:
        if not self._signer.available():
            raise RuntimeError("Lighter signer unavailable; cannot modify order")
        nonce_val = await self._next_nonce()
        tx_info, err = await self._signer.sign_modify_order_tx(
            market_index=market_index,
            order_index=order_index,
            base_amount=base_amount,
            price=price,
            trigger_price=trigger_price,
            nonce=nonce_val,
        )
        if err or not tx_info:
            raise RuntimeError(f"sign_modify_order failed: {err}")
        tx_type = self._signer.get_tx_type_modify_order()
        self._log.info(
            f"Sending modify_order tx (market={market_index}, order_index={order_index}, "
            f"base_amount={base_amount}, price={price})",
            LogColor.BLUE,
        )
        await self._send_ws_or_http(tx_type=tx_type, tx_info=tx_info)

    async def update_leverage(self, market_index: int, fraction_bps: int, margin_mode: int) -> None:
        if not self._signer.available():
            raise RuntimeError("Lighter signer unavailable; cannot update leverage")
        nonce_val = await self._next_nonce()
        tx_info, err = await self._signer.sign_update_leverage_tx(
            market_index=market_index,
            fraction_bps=fraction_bps,
            margin_mode=margin_mode,
            nonce=nonce_val,
        )
        if err or not tx_info:
            raise RuntimeError(f"sign_update_leverage failed: {err}")
        await self._send_ws_or_http(tx_type=self._signer.get_tx_type_update_leverage(), tx_info=tx_info)

    # --- Internal send helpers -------------------------------------------------------
    async def _send_ws_or_http(self, *, tx_type: int, tx_info: str) -> None:
        ws = self._get_ws()
        # Use a 10-minute token horizon; LighterAuthManager auto-refreshes before expiry.
        token = self._auth.token(horizon_secs=600)
        if ws is not None and ws.is_connected():
            await ws.send_jsonapi(
                tx_type=tx_type,
                tx_info_json=tx_info,
                auth=token,
                request_id=f"nautilus-{self._clock.timestamp_ns()}",
            )
            self._log.debug("WS send ok", LogColor.BLUE)
            return
        if self._http_send_fallback:
            await self._http_tx.send_tx(tx_type=tx_type, tx_info=tx_info, auth=token)
            self._log.debug("HTTP send fallback ok", LogColor.BLUE)
            return
        raise RuntimeError("WS not connected and HTTP fallback disabled")

    async def _send_ws_or_http_batch(self, *, tx_types: list[int], tx_infos: list[str]) -> None:
        ws = self._get_ws()
        # Use a 10-minute token horizon; LighterAuthManager auto-refreshes before expiry.
        token = self._auth.token(horizon_secs=600)
        if ws is not None and ws.is_connected():
            await ws.send_jsonapi_batch(
                tx_types=tx_types,
                tx_info_json_strs=tx_infos,
                auth=token,
                request_id=f"nautilus-batch-{self._clock.timestamp_ns()}",
            )
            self._log.debug("WS batch send ok", LogColor.BLUE)
            return
        if self._http_send_fallback:
            await self._http_tx.send_tx_batch(tx_types=tx_types, tx_infos=tx_infos, auth=token)
            self._log.debug("HTTP batch send fallback ok", LogColor.BLUE)
            return
        raise RuntimeError("WS not connected for batch and HTTP fallback disabled")
