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
from collections.abc import Awaitable, Callable
from typing import Any
from weakref import WeakSet

import msgspec
import json as pyjson
import time as _time

import nautilus_trader
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import Logger
from nautilus_trader.common.enums import LogColor
from nautilus_trader.core.nautilus_pyo3 import WebSocketClient, WebSocketClientError, WebSocketConfig, Quota
from nautilus_trader.live.cancellation import cancel_tasks_with_timeout


class LighterWebSocketClient:
    """
    Provides a lightweight WebSocket client for Lighter public channels.

    This thin wrapper around the generic ``WebSocketClient`` adds a simple
    handshake gating mechanism (``type == 'connected'``) and convenient
    subscribe/unsubscribe helpers. It also supports optional server-side
    rate limiting for outgoing messages via ``Quota.rate_per_second``.

    Parameters
    ----------
    clock : LiveClock
        The live clock instance.
    base_url : str
        The base WebSocket URL (``wss://...``). Trailing slashes are trimmed.
    handler : Callable[[bytes], None]
        The callback to receive raw pushed messages.
    handler_reconnect : Callable[..., Awaitable[None]] | None
        Optional callback invoked after a reconnection is established.
    loop : asyncio.AbstractEventLoop
        The event loop used to schedule background tasks.
    ws_send_quota_per_sec : int | None, optional
        Optional quota for outbound messages per second.
    reconnect_timeout_ms : int | None, optional
        Total timeout for reconnect attempts.
    reconnect_delay_initial_ms : int | None, optional
        Initial delay for reconnect backoff.
    reconnect_delay_max_ms : int | None, optional
        Maximum delay for reconnect backoff.
    reconnect_backoff_factor : float | None, optional
        Backoff growth factor between reconnect attempts.
    reconnect_jitter_ms : int | None, optional
        Random jitter applied to reconnect delays.

    References
    ----------
    https://apidocs.lighter.xyz/docs/get-started-for-programmers-1#/
    """

    def __init__(
        self,
        clock: LiveClock,
        base_url: str,
        handler: Callable[[bytes], None],
        handler_reconnect: Callable[..., Awaitable[None]] | None,
        loop: asyncio.AbstractEventLoop,
        ws_send_quota_per_sec: int | None = None,
        reconnect_timeout_ms: int | None = None,
        reconnect_delay_initial_ms: int | None = None,
        reconnect_delay_max_ms: int | None = None,
        reconnect_backoff_factor: float | None = None,
        reconnect_jitter_ms: int | None = None,
    ) -> None:
        self._clock = clock
        # Align logger construction with other adapters (e.g., Binance, Polymarket)
        self._log: Logger = Logger(type(self).__name__)

        self._base_url = base_url.rstrip("/")
        self._url = self._base_url
        self._handler = handler
        self._handler_reconnect = handler_reconnect
        self._loop = loop
        self._tasks: WeakSet[asyncio.Task] = WeakSet()

        self._client: WebSocketClient | None = None
        self._subscriptions: set[str] = set()
        self._ready: bool = False  # True after receiving {"type": "connected"}
        self._ready_evt: asyncio.Event = asyncio.Event()
        # Track whether the last handshake followed an actual reconnect.
        # Initial connection should NOT invoke the external reconnect handler.
        self._did_reconnect: bool = False
        # Rate limit & reconnect
        # Default to cautious WS send rate on testnet to avoid server limits
        if ws_send_quota_per_sec is None and "testnet" in self._base_url.lower():
            self._ws_send_qps = 3
        else:
            self._ws_send_qps = int(ws_send_quota_per_sec) if ws_send_quota_per_sec else None
        self._reconnect_timeout_ms = reconnect_timeout_ms
        self._reconnect_delay_initial_ms = reconnect_delay_initial_ms
        self._reconnect_delay_max_ms = reconnect_delay_max_ms
        self._reconnect_backoff_factor = reconnect_backoff_factor
        self._reconnect_jitter_ms = reconnect_jitter_ms

    @property
    def url(self) -> str:
        """
        Return the server URL being used by the client.

        Returns
        -------
        str
        """
        return self._url

    def is_connected(self) -> bool:
        """
        Return whether the client is connected.

        Returns
        -------
        bool
        """
        return self._client is not None and self._client.is_active()

    def is_disconnected(self) -> bool:
        """
        Return whether the client is disconnected.

        Returns
        -------
        bool
        """
        return not self.is_connected()

    @property
    def is_ready(self) -> bool:
        """
        Return whether the server handshake (``type == 'connected'``) has been received.

        Returns
        -------
        bool
        """
        return self._ready

    async def connect(self) -> None:
        """
        Connect the client to the server and prepare for subscriptions.
        """
        self._log.debug(f"Connecting to {self._url}")
        # Use internal message wrapper to intercept handshake and flush subs
        config = WebSocketConfig(
            url=self._url,
            handler=self._on_message,
            heartbeat=10,
            headers=[("User-Agent", nautilus_trader.NAUTILUS_USER_AGENT)],
            reconnect_timeout_ms=self._reconnect_timeout_ms,
            reconnect_delay_initial_ms=self._reconnect_delay_initial_ms,
            reconnect_delay_max_ms=self._reconnect_delay_max_ms,
            reconnect_backoff_factor=self._reconnect_backoff_factor,
            reconnect_jitter_ms=self._reconnect_jitter_ms,
        )
        if self._ws_send_qps is not None and self._ws_send_qps > 0:
            self._client = await WebSocketClient.connect(
                config=config,
                post_reconnection=self._post_reconnect,
                default_quota=Quota.rate_per_second(int(self._ws_send_qps)),
            )
        else:
            self._client = await WebSocketClient.connect(
                config=config,
                post_reconnection=self._post_reconnect,
            )
        self._log.info(f"Connected to {self._url}", LogColor.BLUE)
        # Subscriptions are flushed after the 'connected' handshake is received.
        # If the server does not send this handshake, subsequent messages still
        # pass to the external handler without blocking.

    def _post_reconnect(self) -> None:
        """
        Handle the Rust-level reconnection callback.

        Resets the readiness flag and event. Subscription flushing (and any
        reconnect handler) is delayed until the handshake is received in
        ``_on_message`` to avoid racing with the server.
        """
        self._log.warning(f"Reconnected to {self._url}")
        self._ready = False
        self._ready_evt = asyncio.Event()
        self._did_reconnect = True
        # Only refresh subscriptions after the 'connected' handshake (see `_on_message`).

    async def disconnect(self) -> None:
        """
        Disconnect the client from the server and cancel background tasks.
        """
        await cancel_tasks_with_timeout(self._tasks, self._log)
        if self._client is None:
            self._log.warning("Cannot disconnect: not connected")
            return
        await self._client.disconnect()
        self._client = None
        self._log.info(f"Disconnected from {self._url}", LogColor.BLUE)

    async def subscribe(self, channel: str) -> None:
        """
        Subscribe to a public channel. If already subscribed, a warning is logged.

        Parameters
        ----------
        channel : str
            The channel name to subscribe to.
        """
        if channel in self._subscriptions:
            self._log.warning(f"Cannot subscribe to {channel}: already subscribed")
            return
        self._subscriptions.add(channel)
        if self._client is not None and self._ready:
            await self._send({"type": "subscribe", "channel": channel})

    async def _subscribe_all(self) -> None:
        # Send iteratively to avoid rate limits; the channel set is typically small.
        if not self._ready:
            return
        for ch in list(self._subscriptions):
            await self._send({"type": "subscribe", "channel": ch})

    async def _send(self, msg: dict[str, Any]) -> None:
        """
        Send a JSON message over the websocket.

        Parameters
        ----------
        msg : dict[str, Any]
            The JSON-serializable message.
        """
        if self._client is None:
            self._log.error(f"Cannot send message {msg}: not connected")
            return
        try:
            # Send as a text frame with JSON-encoded UTF-8 bytes
            json_bytes = pyjson.dumps(msg).encode("utf-8")
            self._log.debug(f"SENDING: {msg}")
            await self._client.send_text(json_bytes)
        except WebSocketClientError as e:
            self._log.error(f"WebSocket send error: {e}")

    async def send(self, msg: dict[str, Any]) -> None:
        """
        Send a JSON message over the websocket (public helper).

        Parameters
        ----------
        msg : dict[str, Any]
            The JSON-serializable message.
        """
        await self._send(msg)

    async def unsubscribe(self, channel: str) -> None:
        """
        Unsubscribe from a public channel and remove it locally.

        Parameters
        ----------
        channel : str
            The channel name to unsubscribe from.
        """
        if channel not in self._subscriptions:
            self._log.warning(f"Cannot unsubscribe from {channel}: not subscribed")
            return
        self._subscriptions.discard(channel)
        if self._client is not None and self._ready:
            await self._send({"type": "unsubscribe", "channel": channel})

    # Internal message wrapper -------------------------------------------------
    def _on_message(self, raw: bytes) -> None:
        """
        Internal message wrapper to intercept the 'connected' handshake.

        The first message from the server typically includes ``{"type":"connected"}``.
        Once received, the client marks itself as ready and flushes any pending
        subscriptions. All messages are then forwarded to the external handler.
        """
        try:
            obj = msgspec.json.decode(raw)
            if isinstance(obj, dict) and obj.get("type") == "connected":
                if not self._ready:
                    self._ready = True
                    if not self._ready_evt.is_set():
                        self._ready_evt.set()
                    self._log.info("WS handshake connected; flushing subscriptions", LogColor.BLUE)
                    # Fire-and-forget: flush subscriptions, then invoke reconnect handler
                    task = self._loop.create_task(self._after_ready())
                    self._tasks.add(task)
                return
        except msgspec.DecodeError:
            # Hand off to external handler (binary or non-JSON frames)
            pass
        except Exception as exc:
            self._log.warning(f"WS handshake parse error: {exc}", LogColor.RED)

        # Forward to external handler
        try:
            self._handler(raw)
        except Exception as e:
            self._log.error(f"WS handler error: {e}")

    async def wait_until_ready(self, timeout_secs: float | None = 10.0) -> bool:
        """
        Await the server handshake (``type == 'connected'``).

        Parameters
        ----------
        timeout_secs : float | None, default 10.0
            Optional timeout in seconds to wait for readiness.

        Returns
        -------
        bool
            ``True`` when ready; ``False`` if the wait timed out.
        """
        if self._ready:
            return True
        try:
            await asyncio.wait_for(self._ready_evt.wait(), timeout=timeout_secs)
            return True
        except asyncio.TimeoutError:
            return False

    # Exposed helpers ---------------------------------------------------------
    async def _after_ready(self) -> None:
        """
        Flush pending subscriptions after handshake and invoke reconnect handler.
        """
        await self._subscribe_all()
        # Invoke the external handler after handshake to allow subscribers to send
        # their messages strictly post-handshake (both on initial connect and reconnect).
        if self._handler_reconnect:
            # Reset flag (best-effort) for future reconnect cycles.
            self._did_reconnect = False
            await self._handler_reconnect()

    @property
    def subscriptions(self) -> list[str]:
        """
        Return the current active subscriptions for the client.

        Returns
        -------
        list[str]
        """
        return sorted(self._subscriptions)

    @property
    def has_subscriptions(self) -> bool:
        """
        Return whether the client has any subscriptions.

        Returns
        -------
        bool
        """
        return bool(self._subscriptions)

    # ---- Private JSONAPI helpers ---------------------------------------------------
    async def send_jsonapi(
        self,
        tx_type: int,
        tx_info_json: str,
        auth: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """
        Send a single JSONAPI transaction over WS (jsonapi/sendtx).

        tx_info_json should be a JSON string encoding an object (not a nested string).
        """
        if self._client is None:
            self._log.error("Cannot send jsonapi/sendtx: not connected")
            return
        try:
            tx_info_obj = msgspec.json.decode(tx_info_json.encode("utf-8"))
        except msgspec.DecodeError as exc:
            self._log.error(f"Invalid tx_info JSON for sendtx: {exc}")
            return
        rid = request_id or f"nautilus-{_time.time_ns()}"
        payload: dict[str, Any] = {
            "type": "jsonapi/sendtx",
            "data": {
                "id": rid,
                "tx_type": int(tx_type),
                "tx_info": tx_info_obj,
            },
        }
        if auth:
            payload["data"]["auth"] = auth
        await self._send(payload)

    async def send_jsonapi_batch(
        self,
        tx_types: list[int],
        tx_info_json_strs: list[str],
        auth: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """
        Send a batch JSONAPI transaction over WS (jsonapi/sendtxbatch).
        """
        if self._client is None:
            self._log.error("Cannot send jsonapi/sendtxbatch: not connected")
            return
        rid = request_id or f"nautilus-batch-{_time.time_ns()}"
        data: dict[str, Any] = {
            "id": rid,
            "tx_types": pyjson.dumps(list(tx_types)),
            "tx_infos": pyjson.dumps(list(tx_info_json_strs)),
        }
        if auth:
            data["auth"] = auth
        payload = {"type": "jsonapi/sendtxbatch", "data": data}
        await self._send(payload)
