from __future__ import annotations

# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
# -------------------------------------------------------------------------------------------------

from typing import Any, Type
from urllib.parse import urlencode

import json as _pyjson
import msgspec
import os
import time

import nautilus_trader
from nautilus_trader.core.nautilus_pyo3 import HttpClient, HttpMethod, HttpResponse, Quota

from .errors import LighterClientError, LighterServerError


class LighterHttpBase:
    """
    Shared HTTP utilities for Lighter REST clients.

    Provides a thin wrapper around the core PyO3 HttpClient with consistent
    headers, error handling and JSON decoding helpers.
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
        default_headers: dict[str, Any] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = HttpClient(
            keyed_quotas=ratelimiter_quotas or [],
            default_quota=ratelimiter_default_quota,
        )
        self._headers: dict[str, Any] = default_headers or {
            "Content-Type": "application/json",
            "User-Agent": nautilus_trader.NAUTILUS_USER_AGENT,
        }
        self._decoder = msgspec.json.Decoder(dict)
        # Retry/backoff are kept for parity; the PyO3 client handles queueing by quota
        self._retry_initial_ms = retry_initial_ms
        self._retry_max_ms = retry_max_ms
        self._retry_backoff_factor = retry_backoff_factor
        self._max_retries = max_retries
        self._timeout_secs = timeout_secs
        # Optional HTTP recorder (JSONL) for diagnostics/schema comparison.
        # Configure via LIGHTER_HTTP_RECORD=/tmp/lighter_http.jsonl
        self._record_http_path: str | None = os.getenv("LIGHTER_HTTP_RECORD")

    def _build_headers(self, auth: str | None = None) -> dict[str, Any]:
        headers: dict[str, Any] = dict(self._headers)
        if auth:
            headers["Authorization"] = str(auth)
        return headers

    def _build_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        if params:
            return f"{self._base_url}{path}?{urlencode(params)}"
        return f"{self._base_url}{path}"

    def _record_http(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        status: int,
        body: bytes | None,
    ) -> None:
        """Best-effort recorder for raw HTTP responses (for offline comparison).

        When LIGHTER_HTTP_RECORD is set, writes one JSONL line per response:
          {
            "ts": <nanos>,
            "method": "GET" | "POST",
            "path": "/api/v1/...",
            "params": {...},
            "status": 200,
            "body": <decoded-json-or-text>
          }
        """
        if not self._record_http_path:
            return
        try:
            ts = int(time.time_ns())
            payload: Any = None
            if body:
                try:
                    payload = self._decoder.decode(body)
                except msgspec.DecodeError:
                    payload = body.decode("utf-8", errors="ignore")
            record = {
                "ts": ts,
                "method": method,
                "path": path,
                "params": params or {},
                "status": int(status),
                "body": payload,
            }
            with open(self._record_http_path, "a", encoding="utf-8") as f:
                f.write(_pyjson.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            # diagnostics-only
            pass

    async def _get_raw(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        auth: str | None = None,
        allow_404: bool = False,
    ) -> HttpResponse | None:
        url = self._build_url(path, params)
        key = f"lighter:{path}"
        response: HttpResponse = await self._client.request(
            HttpMethod.GET,
            url,
            self._build_headers(auth),
            None,
            [key],
            timeout_secs=self._timeout_secs,
        )
        # Record response for diagnostics (even on error)
        self._record_http(
            method="GET",
            path=path,
            params=params,
            status=response.status,
            body=response.body,
        )
        if response.status == 404 and allow_404:
            return None
        if response.status < 400:
            return response
        # Decode error payload best-effort
        try:
            payload = self._decoder.decode(response.body) if response.body else {}
        except msgspec.DecodeError:
            payload = {"message": response.body.decode(errors="ignore")}
        if response.status >= 500:
            raise LighterServerError(status=response.status, message=payload, headers=response.headers)
        else:
            raise LighterClientError(status=response.status, message=payload, headers=response.headers)

    async def _post_raw(
        self,
        path: str,
        body: bytes,
        auth: str | None = None,
        content_type: str | None = None,
    ) -> HttpResponse:
        url = f"{self._base_url}{path}"
        key = f"lighter:{path}"
        headers = self._build_headers(auth)
        if content_type:
            headers["Content-Type"] = content_type
        response: HttpResponse = await self._client.request(
            HttpMethod.POST,
            url,
            headers,
            body,
            [key],
            timeout_secs=self._timeout_secs,
        )
        # Record response for diagnostics (even on error)
        self._record_http(
            method="POST",
            path=path,
            params={},
            status=response.status,
            body=response.body,
        )
        if response.status < 400:
            return response
        try:
            payload = self._decoder.decode(response.body) if response.body else {}
        except msgspec.DecodeError:
            payload = {"message": response.body.decode(errors="ignore")}
        if response.status >= 500:
            raise LighterServerError(status=response.status, message=payload, headers=response.headers)
        else:
            raise LighterClientError(status=response.status, message=payload, headers=response.headers)

    async def _get_json(self, path: str, params: dict[str, Any] | None, struct_type: Type[Any]) -> Any:
        resp = await self._get_raw(path, params)
        dec = msgspec.json.Decoder(struct_type)
        return dec.decode(resp.body) if (resp and resp.body) else struct_type()

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def headers(self) -> dict[str, Any]:
        return self._headers
