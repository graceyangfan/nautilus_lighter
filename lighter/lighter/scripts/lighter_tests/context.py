from __future__ import annotations

import asyncio
import json
import logging
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

import websockets

_ROOT = Path(__file__).resolve().parents[2]
_SITE = _ROOT / ".venv/lib/python3.11/site-packages"
# Prefer local source for adapter code, then site-packages for compiled core
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if _SITE.exists():
    sys.path.append(str(_SITE))

# Import local enums and signer under the official package names to override installed ones
import importlib.util as _import_util
import sys as _sys

_COMMON_DIR = _ROOT / "nautilus_trader" / "adapters" / "lighter" / "common"
_ENUMS_PATH = _COMMON_DIR / "enums.py"
_SIGNER_PATH = _COMMON_DIR / "signer.py"

def _load_as(pkg_name: str, path: Path):
    spec = _import_util.spec_from_file_location(pkg_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module {pkg_name} from {path}")
    mod = _import_util.module_from_spec(spec)
    _sys.modules[pkg_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod

if _SITE.exists():
    _sys.path.insert(0, str(_SITE))

# Load HTTP modules first to avoid package __init__ side effects when signer imports http
_HTTP_DIR = _ROOT / "nautilus_trader" / "adapters" / "lighter" / "http"
_load_as("nautilus_trader.adapters.lighter.http.account", _HTTP_DIR / "account.py")
_load_as("nautilus_trader.adapters.lighter.http.public", _HTTP_DIR / "public.py")
_load_as("nautilus_trader.adapters.lighter.http.transaction", _HTTP_DIR / "transaction.py")

_load_as("nautilus_trader.adapters.lighter.common.enums", _ENUMS_PATH)
_signer_mod = _load_as("nautilus_trader.adapters.lighter.common.signer", _SIGNER_PATH)

LighterCredentials = _signer_mod.LighterCredentials
LighterSigner = _signer_mod.LighterSigner
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.http.transaction import LighterTransactionHttpClient


logger = logging.getLogger("lighter_test")


def _to_int(value: str | int) -> int:
    s = str(value)
    return int(s, 16) if s.startswith("0x") else int(s)


@dataclass(slots=True)
class TestEndpoints:
    http: str
    ws: str


class LighterTestContext:
    """Shared helpers for test scripts interacting with Lighter endpoints."""

    def __init__(
        self,
        *,
        credentials_path: Path,
        base_http: str,
        base_ws: str | None = None,
    ) -> None:
        self._credentials_path = credentials_path
        self._base_http = base_http.rstrip("/")
        self._base_ws = (base_ws or base_http.replace("https", "wss")).rstrip("/") + "/stream"

        creds_doc = json.loads(credentials_path.read_text(encoding="utf-8"))
        self.credentials = LighterCredentials(
            pubkey=str(creds_doc["pubkey"]),
            account_index=_to_int(creds_doc["account_index"]),
            api_key_index=_to_int(creds_doc["api_key_index"]),
            nonce=_to_int(creds_doc.get("nonce", "0x0")),
            private_key=creds_doc.get("private_key"),
        )
        if not self.credentials.private_key:
            raise ValueError("Credentials file must include 'private_key' for automated tests")

        # Infer chain_id if not provided: testnet=300, mainnet=304
        chain_id = 304 if "mainnet" in self._base_http.lower() else 300
        try:
            self.signer = LighterSigner(credentials=self.credentials, base_url=self._base_http, chain_id=chain_id)  # type: ignore[call-arg]
        except TypeError:
            # Fallback for older installed versions without chain_id parameter
            self.signer = LighterSigner(credentials=self.credentials, base_url=self._base_http)  # type: ignore[call-arg]
        self.http_account = LighterAccountHttpClient(self._base_http)
        self.http_public = LighterPublicHttpClient(self._base_http)
        self.http_tx = LighterTransactionHttpClient(self._base_http)

    @property
    def endpoints(self) -> TestEndpoints:
        return TestEndpoints(http=self._base_http, ws=self._base_ws)

    async def fetch_best_prices(self, market_index: int) -> tuple[int | None, int | None]:
        """Return the scaled best bid / ask for a market (None if unavailable)."""
        price_decimals = 0
        try:
            details = await self.http_public.get_order_book_details(market_id=market_index)
            if isinstance(details, dict):
                price_decimals = int(details.get("price_decimals") or details.get("supported_price_decimals") or 0)
        except Exception:
            price_decimals = 0
        try:
            order_book = await self.http_public.get_order_book_orders(market_id=market_index, limit=5)
        except Exception as exc:
            logger.warning("Failed to fetch orderBookOrders: %s", exc)
            return None, None

        def _parse_first(key: str) -> int | None:
            levels = order_book.get(key)
            if not isinstance(levels, list) or not levels:
                return None
            price = levels[0].get("price")
            if price is None:
                return None
            try:
                value = float(str(price))
                scale = 10**price_decimals
                return int(round(value * scale))
            except Exception:
                s = str(price)
                return int(s.replace(".", "")) if "." in s else int(s)

        return _parse_first("bids"), _parse_first("asks")

    async def fetch_mark_price(self, market_index: int) -> int | None:
        """Return last trade price (scaled integer) for the market if available."""
        price_decimals = 0
        try:
            details = await self.http_public.get_order_book_details(market_id=market_index)
        except Exception as exc:
            logger.warning("order_book_details fetch failed: %s", exc)
            return None
        if isinstance(details, dict):
            value = details.get("last_trade_price") or details.get("mark_price")
            if value is not None:
                try:
                    price_decimals = int(details.get("price_decimals") or details.get("supported_price_decimals") or 0)
                    scale = 10**price_decimals
                except Exception:
                    scale = 1
                try:
                    return int(round(float(value) * scale))
                except Exception:
                    return None
        return None

    async def price_limits(
        self,
        market_index: int,
        *,
        is_ask: bool,
        trigger_price: int | None = None,
    ) -> tuple[int | None, int | None]:
        """
        Return (lower, upper) bounds for a price according to Lighter's price checks.

        For asks, only the lower bound is enforced:
            price >= max(markPrice, bestBid) * 0.95
        For bids, only the upper bound is enforced:
            price <= min(markPrice, bestAsk) * 1.05
        """
        mark_price = trigger_price if trigger_price is not None else await self.fetch_mark_price(market_index)
        best_bid, best_ask = await self.fetch_best_prices(market_index)

        if is_ask:
            lower_candidates = [value for value in (mark_price, best_bid) if value is not None]
            upper_candidates = [value for value in (mark_price, best_ask) if value is not None]
            lower = int(max(lower_candidates) * 0.95) if lower_candidates else None
            upper = int(min(upper_candidates) * 1.05) if upper_candidates else None
            lower = max(1, lower) if lower is not None else None
            upper = max(1, upper) if upper is not None else None
            if lower is not None and upper is not None and lower > upper:
                baseline = mark_price or best_ask or best_bid
                if baseline is not None:
                    lower = int(max(1, baseline * 0.95))
                    upper = int(max(1, baseline * 1.05))
                else:
                    lower, upper = None, None
            return lower, upper

        lower_candidates = [value for value in (mark_price, best_bid) if value is not None]
        upper_candidates = [value for value in (mark_price, best_ask) if value is not None]
        lower = int(max(lower_candidates) * 0.95) if lower_candidates else None
        upper = int(min(upper_candidates) * 1.05) if upper_candidates else None
        lower = max(1, lower) if lower is not None else None
        upper = max(1, upper) if upper is not None else None
        if lower is not None and upper is not None and lower > upper:
            baseline = mark_price or best_bid or best_ask
            if baseline is not None:
                lower = int(max(1, baseline * 0.95))
                upper = int(max(1, baseline * 1.05))
            else:
                lower, upper = None, None
        return lower, upper

    async def clamp_price(
        self,
        market_index: int,
        price: int,
        *,
        is_ask: bool,
        trigger_price: int | None = None,
    ) -> int:
        """Clamp price into the allowed range according to price checks."""
        lower, upper = await self.price_limits(
            market_index,
            is_ask=is_ask,
            trigger_price=trigger_price,
        )
        adjusted = price
        if lower is not None:
            adjusted = max(adjusted, lower)
        if upper is not None:
            adjusted = min(adjusted, upper)
        return max(1, adjusted)

    async def fetch_next_nonce(self) -> int:
        payload = await self.http_account.get_next_nonce(
            account_index=self.credentials.account_index,
            api_key_index=self.credentials.api_key_index,
        )
        return int(payload)

    async def run_ws_session(
        self,
        *,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        subscriptions: list[dict[str, Any]] | None = None,
        run_secs: int = 30,
    ) -> None:
        """Utility for opening a short-lived WS session and streaming messages."""
        ssl_ctx = ssl.create_default_context()
        async with websockets.connect(self._base_ws, ssl=ssl_ctx) as ws:
            hello = json.loads(await ws.recv())
            await handler(hello)

            if subscriptions:
                for sub in subscriptions:
                    await ws.send(json.dumps(sub))

            end_time = asyncio.get_event_loop().time() + run_secs
            while asyncio.get_event_loop().time() < end_time:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    await ws.send(json.dumps({"type": "ping"}))
                    continue
                msg = json.loads(raw)
                await handler(msg)
                if msg.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong"}))

    def auth_token(self) -> str:
        """Return a fresh auth token."""
        return self.signer.create_auth_token()

    async def close(self) -> None:
        """Close underlying HTTP clients."""
        for client in (self.http_account, self.http_public, self.http_tx):
            session = getattr(client, "_client", None)
            close_fn = getattr(session, "close", None)
            if close_fn is None:
                continue
            try:
                result = close_fn()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.debug("Ignoring error closing client %s", client, exc_info=True)


def configure_logging(verbose: bool = True) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )
