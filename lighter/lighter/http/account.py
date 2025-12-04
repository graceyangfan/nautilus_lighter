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

from typing import Any

import msgspec

from nautilus_trader.core.nautilus_pyo3 import HttpResponse, Quota
from .base import LighterHttpBase
from nautilus_trader.adapters.lighter.common.normalize import to_float as _norm_to_float
from nautilus_trader.adapters.lighter.common.utils import parse_hex_int as _parse_hex_int
from nautilus_trader.adapters.lighter.http.errors import LighterClientError
from nautilus_trader.adapters.lighter.schemas.ws import (
    LighterAccountOrderStrict,
    LighterAccountPositionStrict,
)
from nautilus_trader.adapters.lighter.schemas.http import (
    LighterAccountOverviewStrict,
    LighterAccountStateStrict,
    LighterAccountOrdersResponseStrict,
    LighterAccountTxsResponse,
    LighterAccountTransaction,
    LighterApiKeysResponse,
    LighterApiKeyItem,
)


class LighterAccountHttpClient(LighterHttpBase):
    """
    Minimal REST client for Lighter account-facing endpoints.

    Currently used for nonce initialisation and backfilling account snapshots.
    """

    def __init__(
        self,
        base_url: str,
        ratelimiter_quotas: list[tuple[str, Quota]] | None = None,
        ratelimiter_default_quota: Quota | None = None,
        retry_initial_ms: int = 100,
        retry_max_ms: int = 5_000,
        retry_backoff_factor: float = 2.0,
        max_retries: int = 3,
        timeout_secs: int = 10,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        super().__init__(
            base_url=self._base_url,
            ratelimiter_quotas=ratelimiter_quotas,
            ratelimiter_default_quota=ratelimiter_default_quota,
            retry_initial_ms=retry_initial_ms,
            retry_max_ms=retry_max_ms,
            retry_backoff_factor=retry_backoff_factor,
            max_retries=max_retries,
            timeout_secs=timeout_secs,
        )
        self._decoder = msgspec.json.Decoder(dict)

    async def _get_raw(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        auth: str | None = None,
        allow_404: bool = False,
    ) -> HttpResponse | None:
        return await super()._get_raw(path, params, auth=auth, allow_404=allow_404)

    async def _get(self, path: str, **params: Any) -> dict:
        auth_val = params.get("auth") if params else None
        resp = await self._get_raw(path, params if params else None, auth=auth_val)
        return self._decoder.decode(resp.body) if (resp and resp.body) else {}

    async def get_next_nonce(self, account_index: int, api_key_index: int) -> int:
        """
        Return the next nonce for the given API key.

        The endpoint currently responds with either ``{"next_nonce": "0x..."}``
        or ``{"next_nonce": 123}`` and both forms are supported.
        """
        payload = await self._get(
            "/api/v1/nextNonce",
            account_index=account_index,
            api_key_index=api_key_index,
        )
        raw = payload.get("next_nonce")
        if raw is None:
            raw = payload.get("nonce")
        parsed = _parse_hex_int(raw)
        if parsed is None:
            raise LighterClientError(status=500, message={"message": "invalid nonce format"}, headers={})
        return parsed

    async def get_account_txs(self, account_index: int, index: int = 0, limit: int = 10, auth: str | None = None) -> list:
        """Fetch account transactions for the given account index (requires auth).

        Returns a typed list of LighterAccountTransaction via msgspec decoding.
        """
        params: dict[str, Any] = {
            "by": "account_index",
            "value": str(account_index),
            "index": int(index),
            "limit": int(limit),
        }
        if auth:
            params["auth"] = auth
        resp = await self._get_raw("/api/v1/accountTxs", params, auth=auth)
        dec = msgspec.json.Decoder(LighterAccountTxsResponse)
        obj = dec.decode(resp.body) if resp and resp.body else LighterAccountTxsResponse()
        txs: list[LighterAccountTransaction] = []
        if obj.data and obj.data.transactions:
            txs = obj.data.transactions
        elif obj.transactions:
            txs = obj.transactions
        return txs

    async def get_account_state(self, account_index: int, auth: str | None = None) -> dict:
        """
        Fetch an account state snapshot for the given account index (requires auth).

        The schema is venue-defined; the execution client maps the payload
        best-effort into ``AccountState`` and stores the raw payload in ``info``.
        """
        params: dict[str, Any] = {"by": "index", "value": str(account_index)}
        if auth:
            params["auth"] = auth
        return await self._get("/api/v1/account", **params)

    async def get_account_overview(self, account_index: int, auth: str | None = None) -> dict:
        """Deprecated: unified strict account API only.

        Use `get_account_state_strict` (or `get_account_overview_strict` alias).
        """
        raise NotImplementedError(
            "get_account_overview is deprecated. Use get_account_state_strict instead."
        )

    async def get_account_overview_strict(self, account_index: int, auth: str | None = None) -> LighterAccountStateStrict:
        """Unified strict account API: use state strict for balances and positions."""
        return await self.get_account_state_strict(account_index=account_index, auth=auth)

    async def get_account_state_strict(self, account_index: int, auth: str | None = None) -> LighterAccountStateStrict:
        """Return a strict typed account state (balances and positions).

        Parses the raw account payload into a consistent structure. Equivalent in
        content to get_account_overview_strict but sourced from the full account
        state endpoint.
        """
        payload = await self.get_account_state(account_index=account_index, auth=auth)
        # Select the matching account object for `account_index`
        account: dict[str, Any] | None = payload.get("account") if isinstance(payload, dict) else None
        if not isinstance(account, dict):
            accounts = payload.get("accounts") if isinstance(payload, dict) else None
            if isinstance(accounts, list) and accounts:
                # attempt to find matching account by common index keys
                selected: dict[str, Any] | None = None
                for acc in accounts:
                    if not isinstance(acc, dict):
                        continue
                    idx_raw = acc.get("account_index") or acc.get("accountId") or acc.get("index") or acc.get("id")
                    idx = _parse_hex_int(idx_raw)
                    if idx is not None and int(idx) == int(account_index):
                        selected = acc
                        break
                account = selected if selected is not None else (accounts[0] if isinstance(accounts[0], dict) else {})
            else:
                account = payload if isinstance(payload, dict) else {}

        # Balances normalization
        avail_raw = (
            account.get("available_balance")
            or account.get("availableBalance")
            or account.get("available")
        )
        total_raw = (
            account.get("total_balance")
            or account.get("totalBalance")
            or account.get("total_asset_value")  # prefer explicit asset value if present
            or account.get("total")
        )

        available_balance = _norm_to_float(avail_raw, 0.0) or 0.0
        total_balance = _norm_to_float(total_raw, available_balance) or available_balance

        # Positions normalization
        positions: list[LighterAccountPositionStrict] = []
        pos_raw = account.get("positions") if isinstance(account, dict) else None
        if isinstance(pos_raw, list):
            for p in pos_raw:
                if not isinstance(p, dict):
                    continue
                mid = p.get("market_id") or p.get("market_index")
                size = p.get("position") or p.get("size")
                if mid is None or size is None:
                    continue
                mid_i = _parse_hex_int(mid)
                if mid_i is None:
                    continue
                aep = p.get("avg_entry_price")
                positions.append(
                    LighterAccountPositionStrict(
                        market_id=mid_i,
                        position=str(size),
                        avg_entry_price=str(aep) if aep is not None else None,
                    )
                )

        return LighterAccountStateStrict(
            available_balance=available_balance,
            total_balance=total_balance,
            positions=positions,
        )

    async def get_account_positions(self, account_index: int, auth: str | None = None) -> list[LighterAccountPositionStrict]:
        """
        Return the current positions for the account (best-effort).

        Lighter testnet exposes positions as part of the account overview payload.
        If no positions are available, an empty list is returned.
        """
        st = await self.get_account_state_strict(account_index=account_index, auth=auth)
        return st.positions

    async def get_account_orders(
        self,
        account_index: int,
        auth: str | None = None,
        market_index: int | None = None,
    ) -> list[LighterAccountOrderStrict]:
        """Return open orders as a strict typed list using msgspec decoding.

        Falls back to [] on 404; otherwise raises for non-2xx responses.
        """
        # Build URL manually to keep raw body for msgspec decoding
        params: dict[str, Any] = {"by": "account_index", "value": str(account_index)}
        if market_index is not None:
            params["market_index"] = int(market_index)
        if auth:
            params["auth"] = auth
        resp = await self._get_raw("/api/v1/accountOrders", params, auth=auth, allow_404=True)
        if resp is None:
            return []
        # Decode flexible response shape into strict structures
        dec = msgspec.json.Decoder(LighterAccountOrdersResponseStrict)
        obj = dec.decode(resp.body) if resp and resp.body else LighterAccountOrdersResponseStrict()

        # Prefer nested data.orders, then flat orders; flatten dict-of-lists if necessary
        results: list[LighterAccountOrderStrict] = []
        if obj.data and obj.data.orders:
            results = obj.data.orders
        elif isinstance(obj.orders, list):
            results = obj.orders
        elif isinstance(obj.orders, dict):
            for arr in obj.orders.values():
                results.extend(arr)
        return results

    async def get_account_active_orders(
        self,
        account_index: int,
        market_index: int,
        auth: str | None = None,
    ) -> list[LighterAccountOrderStrict]:
        """
        Return active (open) orders for the given account and market.

        This wraps the official `/api/v1/accountActiveOrders` endpoint and
        converts the generic `Orders` payload into our strict
        `LighterAccountOrderStrict` structures used by the execution client.
        """
        # Official API expects both header `Authorization` and query `auth`.
        params: dict[str, Any] = {
            "account_index": int(account_index),
            "market_id": int(market_index),
        }
        if auth:
            params["auth"] = auth
        resp = await self._get_raw("/api/v1/accountActiveOrders", params, auth=auth, allow_404=True)
        if resp is None:
            return []

        # Decode to a generic dict first; structure is:
        # { "code": 200, "message": "...", "next_cursor": "...", "orders": [ {...}, ... ] }
        payload = self._decoder.decode(resp.body) if resp.body else {}
        raw_orders = payload.get("orders") or []
        if not isinstance(raw_orders, list):
            return []

        results: list[LighterAccountOrderStrict] = []
        for o in raw_orders:
            if not isinstance(o, dict):
                continue

            try:
                order_index = int(o.get("order_index"))
                market_idx = int(o.get("market_index"))
                status = str(o.get("status") or "")
                is_ask = bool(o.get("is_ask"))
            except Exception:
                # Skip malformed entries
                continue

            client_order_index_val = o.get("client_order_index")
            client_order_index = None
            if client_order_index_val is not None:
                try:
                    client_order_index = int(client_order_index_val)
                except Exception:
                    client_order_index = None

            def _as_str(key: str) -> str | None:
                val = o.get(key)
                if val is None:
                    return None
                return str(val)

            # Map HTTP fields onto the strict ws schema. We intentionally
            # keep only the subset needed for OrderStatusReport generation.
            base_amount = _as_str("initial_base_amount")
            remaining_base_amount = _as_str("remaining_base_amount")
            executed_base_amount = _as_str("filled_base_amount")
            price = _as_str("price")
            trigger_price = _as_str("trigger_price")
            reduce_only_val = o.get("reduce_only")
            reduce_only = bool(reduce_only_val) if reduce_only_val is not None else None

            results.append(
                LighterAccountOrderStrict(
                    order_index=order_index,
                    market_index=market_idx,
                    status=status,
                    is_ask=is_ask,
                    client_order_index=client_order_index,
                    base_amount=base_amount,
                    remaining_base_amount=remaining_base_amount,
                    executed_base_amount=executed_base_amount,
                    filled_size=None,
                    price=price,
                    avg_execution_price=None,
                    reduce_only=reduce_only,
                    trigger_price=trigger_price,
                ),
            )

        return results

    async def get_api_keys(self, account_index: int, api_key_index: int | None = None) -> list[LighterApiKeyItem]:
        """Return API keys registered on the server for the given account (typed)."""
        params: dict[str, Any] = {"account_index": int(account_index)}
        if api_key_index is not None:
            params["api_key_index"] = int(api_key_index)
        resp = await self._get_raw("/api/v1/apikeys", params)
        dec = msgspec.json.Decoder(LighterApiKeysResponse)
        obj = dec.decode(resp.body) if resp and resp.body else LighterApiKeysResponse()
        if obj.data and obj.data.api_keys:
            return obj.data.api_keys
        if obj.api_keys:
            return obj.api_keys
        return []

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def headers(self) -> dict[str, Any]:
        return self._headers
