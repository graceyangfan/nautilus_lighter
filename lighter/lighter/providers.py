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

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.lighter.common.constants import LIGHTER_VENUE
from nautilus_trader.adapters.lighter.common.utils import normalize_filter_values as _normalize
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.schemas.http import (
    LighterOrderBookDetails as _DetailsStruct,
    LighterOrderBookSummary as _SummaryStruct,
)
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.objects import Money
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


@dataclass(slots=True)
class _OrderBookSummary:
    symbol: str
    market_id: int
    status: str
    maker_fee: Decimal
    taker_fee: Decimal
    min_base_amount: Decimal
    min_quote_amount: Decimal
    supported_size_decimals: int
    supported_price_decimals: int
    supported_quote_decimals: int


class LighterInstrumentProvider(InstrumentProvider):
    """
    Load perpetual instruments from Lighter public HTTP API.

    This provider currently treats all markets as USDC-quoted perpetuals and
    emits `CryptoPerpetual` instruments with symbol form `BASEUSDC-PERP`.
    """

    def __init__(
        self,
        client: LighterPublicHttpClient,
        config: InstrumentProviderConfig | None = None,
        concurrency: int = 1,
        default_quote_currency: str = "USDC",
        symbol_suffix: str = "USDC-PERP",
    ) -> None:
        PyCondition.not_none(client, "client")
        super().__init__(config=config or InstrumentProviderConfig())
        self._client = client
        # Default to conservative concurrency (1) to avoid testnet 429s; caller may override
        self._concurrency = max(1, int(concurrency))
        # Default quote/settlement currency (can be overridden if API provides quote info in future)
        self._default_quote_currency = default_quote_currency.upper().strip()
        # Default symbol suffix used to construct final symbol (e.g. "USDC-PERP")
        self._symbol_suffix = symbol_suffix.upper().strip()
        # Cached summaries keyed by market_id for summary-only mapping and on-demand details.
        self._summaries: dict[int, _OrderBookSummary] = {}
        # Fast lookup: market_index -> instrument_id
        self._market_to_instrument: dict[int, InstrumentId] = {}

    # ------------------------------------------------------------------
    # InstrumentProvider interface
    # ------------------------------------------------------------------
    async def load_all_async(self, filters: dict | None = None) -> None:
        """
        Load all active Lighter markets and map them to instruments.

        Parameters
        ----------
        filters : dict | None
            Optional filters to select instruments by base/quote/symbol/kind.
        """
        filters = filters or self._filters

        self._log.debug("Loading Lighter instruments (summary-only)…")
        summaries = await self._load_summaries()

        # Filter out inactive markets early
        summaries = [s for s in summaries if s.status.lower() == "active"]

        # Prefilter summaries by requested symbols/bases to avoid unnecessary detail calls
        if filters:
            summaries = [s for s in summaries if self._summary_accepts(s, filters)]

        # If specific market_ids are requested, filter to only those
        if filters and "market_ids" in filters:
            market_ids = set(filters["market_ids"])
            summaries = [s for s in summaries if s.market_id in market_ids]

        self._log.debug("Active markets to load details: %d", len(summaries))

        # Summary-only mapping: synthesize details from supported_* fields.
        # This keeps HTTP usage low on mainnet while still providing reasonable defaults.
        details_list: list[dict | None] = []
        self._summaries.clear()
        for summary in summaries:
            self._summaries[int(summary.market_id)] = summary
            details_list.append(
                {
                    "price_decimals": summary.supported_price_decimals,
                    "size_decimals": summary.supported_size_decimals,
                    "quote_multiplier": 1,
                    "market_id": summary.market_id,
                    # Margin fractions may be zero here and refined later if needed.
                    "default_initial_margin_fraction": 0,
                    "maintenance_margin_fraction": 0,
                }
            )

        self._reset_caches()

        loaded = 0
        for summary, details in zip(summaries, details_list):
            if not details:
                # Defensive: skip markets with missing synthesized details (should not occur).
                continue

            try:
                instrument = self._map_instrument(summary, details)
            except (KeyError, TypeError, ValueError) as exc:  # defensive parse guard
                self._log.warning(
                    f"Skipping market_id={summary.market_id} ({summary.symbol}): {exc}"
                )
                continue

            if not self._accept_instrument(instrument, filters):
                continue

            self.add_currency(instrument.base_currency)
            self.add_currency(instrument.quote_currency)
            self.add(instrument)
            try:
                mid_val = int(instrument.info["market_id"])  # type: ignore[index]
                self._market_to_instrument[mid_val] = instrument.id
            except (KeyError, TypeError, ValueError):
                pass
            loaded += 1

        if not loaded:
            self._log.warning("No Lighter instruments matched the requested filters")

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        """
        Load a specific set of instruments by ID (best-effort).

        Parameters
        ----------
        instrument_ids : list[InstrumentId]
            The instrument IDs to load.
        filters : dict | None
            Optional filters applied after loading.
        """
        PyCondition.not_none(instrument_ids, "instrument_ids")
        if not instrument_ids:
            return

        for instrument_id in instrument_ids:
            PyCondition.equal(
                instrument_id.venue,
                LIGHTER_VENUE,
                "instrument_id.venue",
                LIGHTER_VENUE.value,
            )

        # Prefer summary-only mapping; if a specific instrument is missing, load details on-demand.
        if not self._summaries:
            await self._load_summaries()  # fill summaries index

        # Try existing provider cache first; if missing, load details using the inferred market_id.
        for instrument_id in instrument_ids:
            if instrument_id in self._instruments:
                continue
            # Parse base symbol (convention: symbol = BASE + suffix).
            sym = instrument_id.symbol.value.upper()
            base = sym.removesuffix(self._symbol_suffix) if sym.endswith(self._symbol_suffix) else sym
            # Locate market_id from summaries using the base symbol.
            found_summary = None
            for s in self._summaries.values():
                if s.symbol.upper() == base:
                    found_summary = s
                    break
            if not found_summary:
                continue
            try:
                details = await self._client.get_order_book_details(found_summary.market_id)
            except Exception as e:  # pragma: no cover
                self._log.warning(f"Failed to fetch details for market_id={found_summary.market_id}: {e}")
                details = None
            if not details:
                # Fallback: synthesize minimal details from the summary.
                details = {
                    "price_decimals": found_summary.supported_price_decimals,
                    "size_decimals": found_summary.supported_size_decimals,
                    "quote_multiplier": 1,
                    "market_id": found_summary.market_id,
                    "default_initial_margin_fraction": 0,
                    "maintenance_margin_fraction": 0,
                }
            try:
                instrument = self._map_instrument(found_summary, details)
            except (KeyError, TypeError, ValueError) as exc:  # pragma: no cover
                self._log.warning(f"Skipping market_id={found_summary.market_id} ({found_summary.symbol}): {exc}")
                continue
            self.add_currency(instrument.base_currency)
            self.add_currency(instrument.quote_currency)
            self.add(instrument)
            try:
                mid_val = int(instrument.info["market_id"])  # type: ignore[index]
                self._market_to_instrument[mid_val] = instrument.id
            except (KeyError, TypeError, ValueError):
                pass

    async def load_async(
        self,
        instrument_id: InstrumentId,
        filters: dict | None = None,
    ) -> None:
        """
        Load a single instrument by ID (best-effort).
        """
        PyCondition.not_none(instrument_id, "instrument_id")
        await self.load_ids_async([instrument_id], filters)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_summaries(self) -> list[_OrderBookSummary]:
        """Fetch and parse the order book summaries catalog."""
        self._log.debug("Fetching order book summaries…")
        raw = await self._client.get_order_books()
        self._log.debug("Retrieved order book summaries: %d", len(raw))

        out: list[_OrderBookSummary] = []
        for item in raw:
            if not isinstance(item, _SummaryStruct):
                continue
            # Direct attribute access; coerce numeric-like fields via Decimal(str(..)) to preserve precision.
            try:
                symbol = str(item.symbol)
                market_id = int(item.market_id)
                status = str(item.status or "active")
                maker_fee = Decimal(str(item.maker_fee if item.maker_fee is not None else "0"))
                taker_fee = Decimal(str(item.taker_fee if item.taker_fee is not None else "0"))
                min_base_amount = Decimal(str(item.min_base_amount if item.min_base_amount is not None else "0"))
                min_quote_amount = Decimal(str(item.min_quote_amount if item.min_quote_amount is not None else "0"))
                supported_size_decimals = int(item.supported_size_decimals or 0)
                supported_price_decimals = int(item.supported_price_decimals or 0)
                supported_quote_decimals = int(item.supported_quote_decimals or 6)
            except (TypeError, ValueError) as exc:  # pragma: no cover
                self._log.warning(f"Unable to parse order book summary: {exc}")
                continue

            out.append(
                _OrderBookSummary(
                    symbol=symbol,
                    market_id=market_id,
                    status=status,
                    maker_fee=maker_fee,
                    taker_fee=taker_fee,
                    min_base_amount=min_base_amount,
                    min_quote_amount=min_quote_amount,
                    supported_size_decimals=supported_size_decimals,
                    supported_price_decimals=supported_price_decimals,
                    supported_quote_decimals=supported_quote_decimals,
                ),
            )

        self._log.debug("Parsed order book summaries: %d", len(out))
        return out

    def _map_instrument(self, summary: _OrderBookSummary, details: Any) -> Instrument:
        # Prefer explicit decimals from details, fall back to supported_* from summary.
        if isinstance(details, _DetailsStruct):
            price_decimals = int(details.price_decimals or summary.supported_price_decimals)
            size_decimals = int(details.size_decimals or summary.supported_size_decimals)
            quote_multiplier = details.quote_multiplier if details.quote_multiplier is not None else 1
            market_id_val = details.market_id if details.market_id is not None else summary.market_id
        else:
            price_decimals = int(details.get("price_decimals", summary.supported_price_decimals))
            size_decimals = int(details.get("size_decimals", summary.supported_size_decimals))
            quote_multiplier = details.get("quote_multiplier", 1)
            market_id_val = details.get("market_id", summary.market_id)

        price_precision = price_decimals
        size_precision = size_decimals

        # Increments derived from decimals.
        price_increment = Price.from_str(str(Decimal(10) ** -price_decimals))
        step_size = Decimal(10) ** -size_decimals
        # Size increment is the discrete step size derived from size_decimals.
        # Minimum tradable quantity is handled separately via `min_quantity`.
        size_increment = Quantity.from_str(str(step_size))

        # Fees: fixed rates for Lighter.
        # Maker: 0.002%, Taker: 0.02%.
        maker_fee = Decimal("0.00002")  # 0.002% = 0.00002
        taker_fee = Decimal("0.0002")   # 0.02% = 0.0002

        # Margin fractions can be bps integers (>1) or decimals (<=1).
        def _to_fraction(v: Any) -> Decimal:
            d = Decimal(str(v))
            if d > 1:
                return d / Decimal(10_000)  # treat as bps
            return d

        if isinstance(details, _DetailsStruct):
            marg_init_v: Any = details.default_initial_margin_fraction if details.default_initial_margin_fraction is not None else 0
            marg_maint_v: Any = details.maintenance_margin_fraction if details.maintenance_margin_fraction is not None else 0
        else:
            marg_init_v = details.get("default_initial_margin_fraction", 0)
            marg_maint_v = details.get("maintenance_margin_fraction", 0)
        margin_init = _to_fraction(marg_init_v)
        margin_maint = _to_fraction(marg_maint_v)

        # Currency mapping: quote/settlement use configured default (e.g., USDC).
        base = summary.symbol.upper()
        # Symbol convention combines base + configured suffix (default "USDC-PERP").
        symbol_value = f"{base}{self._symbol_suffix}"
        instrument_id = InstrumentId(Symbol(symbol_value), LIGHTER_VENUE)
        quote_ccy = Currency.from_str(self._default_quote_currency)

        # Construct instrument.
        ts_now = time.time_ns()
        instrument = CryptoPerpetual(
            instrument_id=instrument_id,
            raw_symbol=Symbol(base),
            base_currency=Currency.from_str(base),
            quote_currency=quote_ccy,
            settlement_currency=quote_ccy,
            is_inverse=False,
            price_precision=price_precision,
            size_precision=size_precision,
            price_increment=price_increment,
            size_increment=size_increment,
            min_quantity=Quantity.from_str(str(summary.min_base_amount)),
            min_notional=Money(summary.min_quote_amount, quote_ccy),
            margin_init=margin_init,
            margin_maint=margin_maint,
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            ts_event=ts_now,
            ts_init=ts_now,
            info={
                "market_id": market_id_val,
                "quote_multiplier": quote_multiplier,
            },
        )
        return instrument

    def _accept_instrument(self, instrument: Instrument, filters: dict | None) -> bool:
        """
        Evaluate instrument against optional filters.

        Reuses a simple filtering approach for consistency with other adapters.
        """
        if not filters:
            return True

        # Only perp markets are produced.
        kinds = _normalize(filters.get("market_types") or filters.get("kinds"), to_lower=True)
        if kinds and "perp" not in kinds:
            return False

        base_code = instrument.base_currency.code
        bases = _normalize(filters.get("bases"))
        if bases and (not base_code or base_code.upper() not in bases):
            return False

        quote_code = instrument.quote_currency.code
        quotes = _normalize(filters.get("quotes"))
        if quotes and (not quote_code or quote_code.upper() not in quotes):
            return False

        symbol_value = instrument.symbol.value
        symbols = _normalize(filters.get("symbols"))
        if symbols and (not symbol_value or symbol_value.upper() not in symbols):
            return False

        return True

    def _reset_caches(self) -> None:
        """Reset internal instrument and currency caches."""
        self._instruments.clear()
        self._currencies.clear()
        self._market_to_instrument = {}

    def default_quote_currency(self) -> Currency:
        """Return the configured default quote/settlement currency."""
        return Currency.from_str(self._default_quote_currency)

    def _summary_accepts(self, summary: _OrderBookSummary, filters: dict | None) -> bool:
        """Lightweight prefilter for summaries using available fields.

        Allows reducing REST detail calls when filters are provided.
        """
        if not filters:
            return True

        # Filter by market_ids if provided.
        market_ids = filters.get("market_ids")
        if market_ids and summary.market_id not in market_ids:
            return False

        bases = _normalize(filters.get("bases"))
        symbols = _normalize(filters.get("symbols"))
        # summary.symbol is treated as BASE code by this provider.
        base = summary.symbol.upper()
        if bases and base not in bases:
            return False
        if symbols:
            target = f"{base}{self._symbol_suffix}"  # final symbol convention for lighter
            if target.upper() not in symbols:
                return False
        return True

    # ------------------------------------------------------------------
    # Public helpers (market index lookup)
    # ------------------------------------------------------------------

    def find_by_market_index(self, market_index: int) -> InstrumentId | None:
        """
        Return an ``InstrumentId`` for the given Lighter ``market_index``.

        If the internal mapping is not yet built, a one-off reverse scan over
        loaded instruments will be performed to populate it.
        """
        try:
            mid = int(market_index)
        except (TypeError, ValueError):
            return None

        inst_id = self._market_to_instrument.get(mid)
        if inst_id is not None:
            return inst_id

        # Fallback: rebuild the mapping once from already-loaded instruments.
        for inst in self._instruments.values():
            try:
                m = int(inst.info["market_id"])  # type: ignore[index]
            except (KeyError, TypeError, ValueError):
                continue
            self._market_to_instrument.setdefault(m, inst.id)

        return self._market_to_instrument.get(mid)
