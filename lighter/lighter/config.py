# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd.
# -------------------------------------------------------------------------------------------------

from __future__ import annotations

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.common.constants import LIGHTER_VENUE
from nautilus_trader.config import LiveDataClientConfig, LiveExecClientConfig
from nautilus_trader.model.identifiers import Venue


class LighterRateLimitConfig:
    """
    Optional rate limit and retry configuration for the Lighter adapter.

    Notes
    -----
    By default no client-side rate limiting is applied and only standard retry
    logic is used for transient HTTP errors. You can provide quotas to enable
    client-side throttling when required.
    """

    def __init__(
        self,
        # HTTP quotas (per-minute)
        http_default_per_minute: int | None = None,
        http_endpoint_per_minute: dict[str, int] | None = None,
        # WS quotas (per-second)
        ws_send_per_second: int | None = None,
        # Retry/backoff
        retry_initial_ms: int = 100,
        retry_max_ms: int = 5_000,
        retry_backoff_factor: float = 2.0,
        max_retries: int = 3,
        # Reconnect (WS)
        ws_reconnect_timeout_ms: int | None = None,
        ws_reconnect_delay_initial_ms: int | None = None,
        ws_reconnect_delay_max_ms: int | None = None,
        ws_reconnect_backoff_factor: float | None = None,
        ws_reconnect_jitter_ms: int | None = None,
    ) -> None:
        self.http_default_per_minute = http_default_per_minute
        self.http_endpoint_per_minute = http_endpoint_per_minute or {}
        self.ws_send_per_second = ws_send_per_second
        self.retry_initial_ms = int(retry_initial_ms)
        self.retry_max_ms = int(retry_max_ms)
        self.retry_backoff_factor = float(retry_backoff_factor)
        self.max_retries = int(max_retries)
        self.ws_reconnect_timeout_ms = ws_reconnect_timeout_ms
        self.ws_reconnect_delay_initial_ms = ws_reconnect_delay_initial_ms
        self.ws_reconnect_delay_max_ms = ws_reconnect_delay_max_ms
        self.ws_reconnect_backoff_factor = ws_reconnect_backoff_factor
        self.ws_reconnect_jitter_ms = ws_reconnect_jitter_ms


class LighterDataClientConfig(LiveDataClientConfig, frozen=True):
    """
    Configuration for ``LighterDataClient`` instances.

    Parameters
    ----------
    base_url_http : str, optional
        The REST base URL used for instrument snapshots.
    base_url_ws : str, optional
        The WebSocket base URL for public streams.
    venue : Venue, default LIGHTER_VENUE
        The venue identifier.
    ratelimit : LighterRateLimitConfig, optional
        Optional client-side rate limit configuration reused by the data client.
    """

    base_url_http: str | None = None
    base_url_ws: str | None = None
    venue: Venue = LIGHTER_VENUE
    ratelimit: LighterRateLimitConfig | None = None


class LighterExecClientConfig(LiveExecClientConfig, frozen=True):
    """
    Configuration for ``LighterExecutionClient`` instances.

    Parameters
    ----------
    base_url_http : str, optional
        The REST base URL.
    base_url_ws : str, optional
        The WebSocket base URL.
    credentials : LighterCredentials, optional
        Strongly-typed credentials; prefer passing this field instead of JSON paths.
    venue : Venue, default LIGHTER_VENUE
        The venue identifier.

    Notes
    -----
    Do not persist private keys in code. A signer backend can use `LIGHTER_PRIVATE_KEY`
    from the environment to avoid storing secrets at rest.
    """

    base_url_http: str | None = None
    base_url_ws: str | None = None
    # Optional chain ID for the signer (304 mainnet / 300 testnet). If None, inferred from base_url.
    chain_id: int | None = None
    # Prefer passing typed credentials directly; JSON file paths are discouraged.
    credentials: LighterCredentials | None = None
    # Deprecated compatibility field (kept as a placeholder for older configs)
    credentials_path: str | None = None
    venue: Venue = LIGHTER_VENUE
    # Optional rate limit + retry configuration
    ratelimit: LighterRateLimitConfig | None = None
    # Whether to subscribe to account_stats/{account} channel (some deployments disable it)
    # Default False per recent testnet observations; rely on HTTP snapshot for balances.
    subscribe_account_stats: bool = False
    # Debug/diagnostics: after submit, attempt short HTTP reconciliation to backfill
    # order_index via client_order_index when WS callbacks are delayed (testnet).
    post_submit_http_reconcile: bool = False
    post_submit_poll_attempts: int = 3
    post_submit_poll_interval_ms: int = 1500
    # Use HTTP sendTx fallback when WS send fails (simplify flow by default)
    http_send_fallback: bool = False
    # Use Python websockets fallback for private account channels (align with lighter-python behavior)
    use_python_ws_private: bool = False
    # De-duplication tuning (advanced)
    # Maximum remembered trade_ids for FillReport de-duplication
    trade_dedup_capacity: int = 10_000
    # Optional short suppression window for position duplicates (ms). If None, value-based only.
    position_dedup_suppress_window_ms: int | None = None
