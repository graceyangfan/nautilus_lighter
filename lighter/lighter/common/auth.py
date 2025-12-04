from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .signer import LighterSigner


@dataclass(slots=True)
class LighterAuthManager:
    """Lightweight Auth token manager for short‑lived Lighter tokens.

    Caches a token and refreshes it ahead of expiry to avoid frequent re‑signing.
    """

    signer: LighterSigner
    default_horizon_secs: int = 10 * 60
    refresh_buffer_secs: int = 60

    _token: Optional[str] = None
    _expires_at: int = 0

    def token(self, horizon_secs: Optional[int] = None, force: bool = False) -> str:
        """Return a valid token, refreshing if necessary.

        This method is synchronous because the underlying signer call is local
        (ctypes into lighter-go) and does not perform I/O.
        """
        now = int(time.time())
        if (not force) and self._token and now < self._expires_at - self.refresh_buffer_secs:
            return self._token
        return self.refresh(horizon_secs=horizon_secs)

    def refresh(self, horizon_secs: Optional[int] = None) -> str:
        """Force-generate a new token regardless of cache state."""
        now = int(time.time())
        horizon = int(horizon_secs or self.default_horizon_secs)
        tok = self.signer.create_auth_token(deadline_seconds=horizon)
        self._token = tok or ""
        self._expires_at = now + horizon
        return self._token
