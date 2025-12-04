from __future__ import annotations

# -------------------------------------------------------------------------------------------------
#  Utilities for normalizing price/quantity and parsing numeric values
#  Aligned in spirit with Hyperliquid adapter helpers (round-down semantics
#  and decimal-first conversions), but adapted for Lighter needs (integer scaling).
# -------------------------------------------------------------------------------------------------

from decimal import Decimal, ROUND_DOWN, DecimalException
from typing import Any
from nautilus_trader.adapters.lighter.common.utils import parse_hex_int as _parse_hex_int


def to_decimal(value: Any) -> Decimal | None:
    """Convert supported numeric-like inputs to Decimal.

    Accepted inputs:
    - None -> None
    - Decimal -> Decimal
    - int/float -> Decimal(str(value))
    - str -> Decimal(value)
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except DecimalException:
            return None
    return None


def to_float(value: Any, default: float | None = None) -> float | None:
    """Convert arbitrary numeric-like input to float using Decimal normalization."""
    dec = to_decimal(value)
    if dec is None:
        return default
    try:
        return float(dec)
    except (ValueError, OverflowError):
        return default


def parse_int(value: Any) -> int | None:
    """Parse an integer from int/str, with 0x-hex support; else None."""
    return _parse_hex_int(value)


def normalize_price_decimal(price: Decimal, decimals: int) -> Decimal:
    """Floor the price to the given decimal places."""
    if decimals <= 0:
        # No decimals: floor to integer
        return price.to_integral_value(rounding=ROUND_DOWN)
    # Quantize to specified decimals with ROUND_DOWN
    exp = Decimal(1).scaleb(-decimals)  # 10^-decimals
    return price.quantize(exp, rounding=ROUND_DOWN)


def normalize_quantity_decimal(qty: Decimal, decimals: int) -> Decimal:
    """Floor the quantity to the given decimal places."""
    if decimals <= 0:
        return qty.to_integral_value(rounding=ROUND_DOWN)
    exp = Decimal(1).scaleb(-decimals)
    return qty.quantize(exp, rounding=ROUND_DOWN)


def decimal_to_scaled_int(value: Decimal, decimals: int) -> int:
    """Scale a Decimal to an integer by 10^decimals after prior normalization."""
    factor = Decimal(10) ** decimals
    return int((value * factor).to_integral_value(rounding=ROUND_DOWN))


def normalize_price_to_int(
    price: Any | None,
    *,
    price_decimals: int,
    price_increment: Any | None = None,
) -> tuple[int | None, Decimal | None]:
    """Normalize and scale a price according to increment and decimals."""
    dec = to_decimal(price)
    if dec is None:
        return None, None
    inc = to_decimal(price_increment)
    if inc is not None and inc > 0:
        dec = round_down_to_tick(dec, inc)
    normalized = normalize_price_decimal(dec, price_decimals)
    return decimal_to_scaled_int(normalized, price_decimals), normalized


def normalize_size_to_int(
    size: Any | None,
    *,
    size_decimals: int,
    size_increment: Any | None = None,
) -> tuple[int | None, Decimal | None]:
    """Normalize and scale a size according to increment and decimals."""
    dec = to_decimal(size)
    if dec is None:
        return None, None
    inc = to_decimal(size_increment)
    if inc is not None and inc > 0:
        dec = round_down_to_step(dec, inc)
    normalized = normalize_quantity_decimal(dec, size_decimals)
    return decimal_to_scaled_int(normalized, size_decimals), normalized


# --- Tick/Step helpers (Hyperliquid-style rounding) ---------------------------------------------

def round_down_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    if tick_size == 0:
        return price
    # floor(price / tick) * tick
    return (price / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size


def round_down_to_step(qty: Decimal, step_size: Decimal) -> Decimal:
    if step_size == 0:
        return qty
    return (qty / step_size).to_integral_value(rounding=ROUND_DOWN) * step_size


def ensure_min_notional(price: Decimal, qty: Decimal, min_notional: Decimal) -> bool:
    return (price * qty) >= min_notional


def money_to_decimal(money: Any) -> Decimal | None:
    """Extract Decimal amount from a Money instance.

    Assumes `money` is a Nautilus `Money` type; raises if not.
    """
    if money is None:
        return None
    # Direct attribute access; rely on Money API consistency
    return Decimal(str(money.as_decimal()))
