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

from typing import Any
import msgspec
import time as _time

from nautilus_trader.core.nautilus_pyo3 import HttpResponse, Quota
from .base import LighterHttpBase
from nautilus_trader.adapters.lighter.schemas.http import (
    LighterOrderBooksResponse,
    LighterOrderBookDetailsResponse,
)


class LighterPublicHttpClient(LighterHttpBase):
    """
    Minimal async HTTP client for Lighter public endpoints used by the
    instrument provider. This client is intentionally tiny and easy to mock in
    tests.
    """

    def __init__(
        self,
        base_url: str,
        timeout_secs: int = 10,
        ratelimiter_quotas: list[tuple[str, Quota]] | None = None,
        ratelimiter_default_quota: Quota | None = None,
        retry_initial_ms: int = 100,
        retry_max_ms: int = 5_000,
        retry_backoff_factor: float = 2.0,
        max_retries: int = 3,
        orders_cache_ttl_secs: int | None = None,
    ) -> None:
        # Apply default cautious quotas for testnet if none provided
        self._base_url = base_url.rstrip("/")
        if ratelimiter_quotas is None and "testnet" in self._base_url.lower():
            ratelimiter_quotas = [
                ("lighter:/api/v1/orderBooks", Quota.rate_per_minute(6)),
                ("lighter:/api/v1/orderBookDetails", Quota.rate_per_minute(6)),
            ]
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
        # Lightweight in-memory cache for orderBookOrders (reduce test HTTP)
        # Enable small TTL by default on testnet if not provided
        self._orders_cache_ttl_secs = orders_cache_ttl_secs
        if self._orders_cache_ttl_secs is None and "testnet" in (self._base_url or "").lower():
            self._orders_cache_ttl_secs = 2
        self._orders_cache: dict[int, tuple[float, dict]] = {}
    
    async def _get(self, path: str, **params: Any) -> dict:
        resp = await self._get_raw(path, params if params else None)
        return self._decoder.decode(resp.body) if (resp and resp.body) else {}

    # ----- Endpoints -----------------------------------------------------

    async def _get_raw(self, path: str, params: dict[str, Any] | None = None) -> HttpResponse:
        resp = await super()._get_raw(path, params)
        return resp

    async def _get_json(self, path: str, params: dict[str, Any] | None, struct_type: type) -> Any:
        resp = await self._get_raw(path, params)
        dec = msgspec.json.Decoder(struct_type)
        return dec.decode(resp.body) if resp.body else struct_type()

    async def get_order_books(self):
        """
        Return the order books listing (market catalog).

        Response shape:
        {"code": 200, "order_books": [ { ... }, ... ]}
        """
        resp = await self._get_json("/api/v1/orderBooks", None, LighterOrderBooksResponse)
        return resp.order_books

    async def get_order_book_details(self, market_id: int):
        """
        Return detailed metadata for a single market ID.

        Response shape:
        {"code": 200, "order_book_details": [ { ... } ]}
        """
        parsed = await self._get_json(
            "/api/v1/orderBookDetails",
            {"market_id": market_id},
            LighterOrderBookDetailsResponse,
        )
        first = parsed.order_book_details[0] if parsed.order_book_details else None
        return first

    async def get_order_book_orders(self, market_id: int, limit: int | None = None) -> dict:
        """
        Return raw order book orders for a market (snapshot source).

        Response shape:
        {"code":200, "total_asks":INT, "asks":[{price,size or remaining_base_amount,...}], "total_bids":INT, "bids":[...]}
        """
        params: dict[str, Any] = {"market_id": market_id}
        if limit is not None:
            params["limit"] = limit
        # Cache hit within TTL
        ttl = self._orders_cache_ttl_secs or 0
        if ttl > 0:
            now = _time.time()
            cached = self._orders_cache.get(int(market_id))
            if cached and (now - cached[0]) <= ttl:
                return cached[1]
            resp = await self._get_raw("/api/v1/orderBookOrders", params)
            data = self._decoder.decode(resp.body) if (resp and resp.body) else {}
            self._orders_cache[int(market_id)] = (now, data)
            return data
        else:
            resp = await self._get_raw("/api/v1/orderBookOrders", params)
            return self._decoder.decode(resp.body) if (resp and resp.body) else {}

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def headers(self) -> dict[str, Any]:
        return self._headers
