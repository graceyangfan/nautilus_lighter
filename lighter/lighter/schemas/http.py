from __future__ import annotations

# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
# -------------------------------------------------------------------------------------------------

import msgspec
from typing import List, Any

from nautilus_trader.adapters.lighter.schemas.ws import (
    LighterAccountPositionStrict,
    LighterAccountOrderStrict,
)


class LighterOrderBookSummary(msgspec.Struct, frozen=True, omit_defaults=True):
    symbol: str
    market_id: int
    status: str | None = None
    maker_fee: object | None = None
    taker_fee: object | None = None
    min_base_amount: object | None = None
    min_quote_amount: object | None = None
    supported_size_decimals: int | None = None
    supported_price_decimals: int | None = None
    supported_quote_decimals: int | None = None


class LighterOrderBooksResponse(msgspec.Struct, frozen=True, omit_defaults=True):
    code: int | None = None
    order_books: list[LighterOrderBookSummary] = []


class LighterOrderBookDetails(msgspec.Struct, frozen=True, omit_defaults=True):
    market_id: int | None = None
    price_decimals: int | None = None
    size_decimals: int | None = None
    quote_multiplier: object | None = None
    default_initial_margin_fraction: object | None = None
    maintenance_margin_fraction: object | None = None


class LighterOrderBookDetailsResponse(msgspec.Struct, frozen=True, omit_defaults=True):
    code: int | None = None
    order_book_details: list[LighterOrderBookDetails] = []


# Account transactions ---------------------------------------------------------------------------

class LighterAccountTransaction(msgspec.Struct, frozen=True, omit_defaults=True):
    client_order_index: int | None = None
    order_index: int | None = None
    market_index: int | None = None
    market_id: int | None = None


class LighterAccountTxsData(msgspec.Struct, frozen=True, omit_defaults=True):
    transactions: list[LighterAccountTransaction] = []


class LighterAccountTxsResponse(msgspec.Struct, frozen=True, omit_defaults=True):
    code: int | None = None
    transactions: list[LighterAccountTransaction] | None = None
    data: LighterAccountTxsData | None = None


# API keys ----------------------------------------------------------------------------------------

class LighterApiKeyItem(msgspec.Struct, frozen=True, omit_defaults=True):
    account_index: int | None = None
    api_key_index: int | None = None
    nonce: int | None = None
    public_key: str | None = None


class LighterApiKeysData(msgspec.Struct, frozen=True, omit_defaults=True):
    api_keys: list[LighterApiKeyItem] = []


class LighterApiKeysResponse(msgspec.Struct, frozen=True, omit_defaults=True):
    code: int | None = None
    api_keys: list[LighterApiKeyItem] | None = None
    data: LighterApiKeysData | None = None


class LighterAccountOverviewStrict(msgspec.Struct, frozen=True, omit_defaults=True):
    """Strict typed account overview returned by HTTP API.

    Fields are normalized/coerced in the HTTP client before constructing this struct.
    """

    available_balance: float
    total_balance: float
    positions: List[LighterAccountPositionStrict] = []


class LighterAccountStateStrict(msgspec.Struct, frozen=True, omit_defaults=True):
    """Strict typed account state (balances + positions).

    Mirrors LighterAccountOverviewStrict for clarity in call sites which refer
    to the full account state snapshot terminology.
    """

    available_balance: float
    total_balance: float
    positions: List[LighterAccountPositionStrict] = []


class LighterAccountOrdersDataStrict(msgspec.Struct, frozen=True, omit_defaults=True):
    orders: list[LighterAccountOrderStrict] = []


class LighterAccountOrdersResponseStrict(msgspec.Struct, frozen=True, omit_defaults=True):
    code: int | None = None
    # API can return either a flat list of orders, or a dict of lists keyed by e.g. market id
    orders: list[LighterAccountOrderStrict] | dict[str, list[LighterAccountOrderStrict]] | None = None
    data: LighterAccountOrdersDataStrict | None = None
