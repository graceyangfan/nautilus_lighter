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

import json
from typing import Any, Iterable
from urllib.parse import urlencode

import msgspec

from nautilus_trader.core.nautilus_pyo3 import HttpResponse, Quota
from .base import LighterHttpBase


class LighterTransactionHttpClient(LighterHttpBase):
    """Thin REST wrapper around the Lighter ``sendTx`` endpoints."""

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
            default_headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        self._decoder = msgspec.json.Decoder(dict)

    def _encode_form(self, entries: Iterable[tuple[str, str]]) -> bytes:
        return urlencode(list(entries)).encode("utf-8")

    def _build_headers(self, auth: str | None = None) -> dict[str, Any]:
        """Return request headers, optionally including Authorization."""
        headers = dict(self._headers)
        if auth:
            headers["Authorization"] = str(auth)
        return headers

    async def _post_form(self, endpoint: str, form: Iterable[tuple[str, str]], auth: str | None = None) -> dict:
        """POST URL-encoded form to the given endpoint and return decoded JSON or raise.

        Centralizes Authorization header wiring, ratelimiter key, and error decoding.
        """
        body = self._encode_form(form)
        resp = await self._post_raw(endpoint, body, auth=auth, content_type="application/x-www-form-urlencoded")
        return self._decoder.decode(resp.body) if (resp and resp.body) else {}

    async def send_tx(
        self,
        tx_type: int,
        tx_info: str,
        price_protection: bool | None = None,
        auth: str | None = None,
    ) -> dict:
        """Invoke ``POST /api/v1/sendTx`` with a signed transaction payload."""
        form: list[tuple[str, str]] = [
            ("tx_type", str(tx_type)),
            ("tx_info", str(tx_info)),
        ]
        if price_protection is not None:
            form.append(("price_protection", "true" if price_protection else "false"))
        if auth:
            form.append(("auth", auth))
        return await self._post_form("/api/v1/sendTx", form, auth=auth)

    async def send_tx_batch(
        self,
        tx_types: list[int],
        tx_infos: list[str],
        auth: str | None = None,
    ) -> dict:
        """Invoke ``POST /api/v1/sendTxBatch`` to submit a batch of transactions."""

        # Lighter expects the tx_types/tx_infos payloads to be JSON-encoded strings
        types_json = json.dumps(list(tx_types))
        infos_json = json.dumps(list(tx_infos))
        form: list[tuple[str, str]] = [
            ("tx_types", types_json),
            ("tx_infos", infos_json),
        ]
        if auth:
            form.append(("auth", auth))
        return await self._post_form("/api/v1/sendTxBatch", form, auth=auth)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def headers(self) -> dict[str, Any]:
        return self._headers
