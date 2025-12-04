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

from nautilus_trader.core.nautilus_pyo3 import Quota

from nautilus_trader.adapters.lighter.config import (
    LighterDataClientConfig,
    LighterExecClientConfig,
    LighterRateLimitConfig,
)
from nautilus_trader.adapters.lighter.data import LighterDataClient
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory


def _assemble_limits(ratelimit: LighterRateLimitConfig | None):
    """Build quotas/retry/WS kwargs from LighterRateLimitConfig (shared by factories)."""
    # Defaults to conservative values if None
    rl = ratelimit or LighterRateLimitConfig(
        http_default_per_minute=6,
        ws_send_per_second=3,
        retry_initial_ms=200,
        retry_max_ms=3000,
        retry_backoff_factor=2.0,
        max_retries=2,
    )
    http_keyed_quotas = []
    http_default_quota = None
    retry_kwargs = {}
    ws_kwargs = {}
    if rl.http_default_per_minute:
        http_default_quota = Quota.rate_per_minute(int(rl.http_default_per_minute))
    if rl.http_endpoint_per_minute:
        for ep, per_min in rl.http_endpoint_per_minute.items():
            http_keyed_quotas.append((f"lighter:{ep}", Quota.rate_per_minute(int(per_min))))
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
    return http_keyed_quotas, http_default_quota, retry_kwargs, ws_kwargs


def get_lighter_instrument_provider(
    client: LighterPublicHttpClient,
    base_http: str,
    config: InstrumentProviderConfig,
) -> LighterInstrumentProvider:
    """Construct a LighterInstrumentProvider with sensible defaults.

    - Defaults to low concurrency on testnet to reduce 429s.
    - Ensures a summary-only catalog will be loaded on start when users did not
      specify `load_all` or `load_ids`.
    """
    default_conc = 1 if "testnet" in (base_http or "").lower() else 4
    prov_cfg = config
    if (not prov_cfg.load_all) and not prov_cfg.load_ids:
        prov_cfg = InstrumentProviderConfig(load_all=True, filters=prov_cfg.filters)
    return LighterInstrumentProvider(client=client, concurrency=default_conc, config=prov_cfg)


class LighterLiveExecClientFactory(LiveExecClientFactory):
    """Factory for creating ``LighterExecutionClient`` instances."""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LighterExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> LighterExecutionClient:
        base_http = config.base_url_http or "https://testnet.zklighter.elliot.ai"
        base_ws = config.base_url_ws or "wss://testnet.zklighter.elliot.ai/stream"

        http_keyed_quotas, http_default_quota, retry_kwargs, ws_kwargs = _assemble_limits(config.ratelimit)

        http_public = LighterPublicHttpClient(
            base_http,
            ratelimiter_quotas=http_keyed_quotas,
            ratelimiter_default_quota=http_default_quota,
            **retry_kwargs,
        )
        provider = get_lighter_instrument_provider(
            client=http_public,
            base_http=base_http,
            config=config.instrument_provider,
        )

        # Require explicit typed credentials; do not load from JSON paths here
        if config.credentials is None:
            raise ValueError("LighterExecClientConfig.credentials is required; do not use JSON path")

        client = LighterExecutionClient(
            loop=loop,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            base_url_http=base_http,
            base_url_ws=base_ws,
            credentials=config.credentials,  # type: ignore[arg-type]
            config=config,
        )
        # Attach WS config for send quota/reconnect (consumed on connect in execution client)
        client._ws_config_overrides = ws_kwargs  # type: ignore[attr-defined]
        return client


class LighterLiveDataClientFactory(LiveDataClientFactory):
    """Factory for creating ``LighterDataClient`` instances."""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LighterDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> LighterDataClient:
        base_http = config.base_url_http or "https://testnet.zklighter.elliot.ai"
        base_ws = config.base_url_ws or "wss://testnet.zklighter.elliot.ai/stream"

        http_keyed_quotas, http_default_quota, retry_kwargs, ws_kwargs = _assemble_limits(config.ratelimit)

        http_public = LighterPublicHttpClient(
            base_http,
            ratelimiter_quotas=http_keyed_quotas,
            ratelimiter_default_quota=http_default_quota,
            **retry_kwargs,
        )
        provider = get_lighter_instrument_provider(
            client=http_public,
            base_http=base_http,
            config=config.instrument_provider,
        )

        return LighterDataClient(
            loop=loop,
            http=http_public,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            base_url_ws=base_ws,
            name=name,
            config=config,
        )
