from __future__ import annotations

"""Venue-specific enums and mapping helpers for the Lighter adapter.

These enums mirror the transaction and order types used by the lighter-go
signing library and the venue API. Execution code maps Nautilus enums to the
Lighter variants, while the signer accepts only Lighter-specific values.
"""

from enum import IntEnum
from nautilus_trader.model.enums import OrderType, TimeInForce, OrderStatus


class LighterOrderType(IntEnum):
    LIMIT = 0
    MARKET = 1
    STOP_LOSS = 2
    STOP_LOSS_LIMIT = 3
    TAKE_PROFIT = 4
    TAKE_PROFIT_LIMIT = 5
    TWAP = 6


class LighterTimeInForce(IntEnum):
    IOC = 0
    GTT = 1
    POST_ONLY = 2


class LighterCancelAllTif(IntEnum):
    IMMEDIATE = 0
    SCHEDULED = 1
    ABORT = 2


class LighterMarginMode(IntEnum):
    CROSS = 0
    ISOLATED = 1


class LighterEnumParser:
    """Translate Nautilus enums to Lighter enums.

    This mirrors the approach used in other adapters: the execution layer is
    responsible for mapping and validation, and the signer only consumes the
    venue-native types.
    """

    @staticmethod
    def parse_nautilus_order_type(order_type: OrderType) -> LighterOrderType:
        if order_type == OrderType.LIMIT:
            return LighterOrderType.LIMIT
        if order_type == OrderType.MARKET:
            return LighterOrderType.MARKET
        # Map Nautilus STOP/IF_TOUCHED to Lighter STOP_LOSS/TAKE_PROFIT families
        if order_type == OrderType.STOP_MARKET:
            return LighterOrderType.STOP_LOSS
        if order_type == OrderType.STOP_LIMIT:
            return LighterOrderType.STOP_LOSS_LIMIT
        if order_type == OrderType.MARKET_IF_TOUCHED:
            return LighterOrderType.TAKE_PROFIT
        if order_type == OrderType.LIMIT_IF_TOUCHED:
            return LighterOrderType.TAKE_PROFIT_LIMIT
        # Trailing and other exotic types are not supported yet
        raise RuntimeError(f"Unsupported order type for Lighter: {order_type}")

    @staticmethod
    def parse_nautilus_time_in_force(time_in_force: TimeInForce, post_only: bool) -> LighterTimeInForce:
        if post_only:
            return LighterTimeInForce.POST_ONLY
        if time_in_force in (TimeInForce.IOC, TimeInForce.FOK):
            return LighterTimeInForce.IOC
        # All other cases (GTC/GTD/DAY) are treated as GTT at the venue
        return LighterTimeInForce.GTT

    # ---- Helpers for order type semantics ---------------------------------------------
    @staticmethod
    def requires_price(order_type: LighterOrderType) -> bool:
        """Return True if the Lighter order type requires a price field."""
        return order_type in (
            LighterOrderType.LIMIT,
            LighterOrderType.STOP_LOSS_LIMIT,
            LighterOrderType.TAKE_PROFIT_LIMIT,
        )

    @staticmethod
    def requires_trigger_price(order_type: LighterOrderType) -> bool:
        """Return True if the Lighter order type requires a trigger price field."""
        return order_type in (
            LighterOrderType.STOP_LOSS,
            LighterOrderType.STOP_LOSS_LIMIT,
            LighterOrderType.TAKE_PROFIT,
            LighterOrderType.TAKE_PROFIT_LIMIT,
        )

    # ---- Order status normalization ---------------------------------------------------
    @staticmethod
    def parse_order_status(status: str | None) -> OrderStatus | None:
        """Map venue status text to Nautilus OrderStatus.

        This is aligned with lighter-python's `Order.status` enum
        (see `/tmp/lighter-python-official/lighter/models/order.py`).

        Official statuses include (non-exhaustive):
        - Active states: ``in-progress``, ``pending``, ``open``
        - Terminal filled: ``filled``
        - Terminal canceled variants:
          ``canceled``, ``canceled-post-only``, ``canceled-reduce-only``,
          ``canceled-position-not-allowed``, ``canceled-margin-not-allowed``,
          ``canceled-too-much-slippage``, ``canceled-not-enough-liquidity``,
          ``canceled-self-trade``, ``canceled-expired``, ``canceled-oco``,
          ``canceled-child``, ``canceled-liquidation``.
        """
        if not status:
            return None
        s_raw = str(status).strip()
        s = s_raw.upper()
        s_lower = s_raw.lower()

        # Active / working states
        if s_lower in {"open", "active", "resting", "new", "accepted", "pending", "in-progress", "in_progress"}:
            return OrderStatus.ACCEPTED

        # Partial fill variants (not currently observed on Lighter, but kept for safety)
        if s_lower in {"partially_filled", "partially-filled", "partial", "part_fills"}:
            return OrderStatus.PARTIALLY_FILLED

        # Explicit expired + venue-specific "canceled-expired"
        if s_lower == "expired" or s_lower == "canceled-expired":
            return OrderStatus.EXPIRED

        # All other canceled-* variants should be treated as CANCELED
        if s_lower.startswith("canceled") or s_lower.startswith("cancelled"):
            return OrderStatus.CANCELED

        # Fully filled
        if s_lower in {"filled", "executed"}:
            return OrderStatus.FILLED

        # Generic rejection / failure markers
        if "reject" in s_lower or s_lower in {"failed", "invalid"}:
            return OrderStatus.REJECTED

        return None


# Expiry conventions (aligned with lighter-python SignerClient)
DEFAULT_IOC_EXPIRY: int = 0
DEFAULT_GTT_EXPIRY: int = -1


class LighterTxType(IntEnum):
    CHANGE_PUB_KEY = 8
    CREATE_SUB_ACCOUNT = 9
    CREATE_PUBLIC_POOL = 10
    UPDATE_PUBLIC_POOL = 11
    TRANSFER = 12
    WITHDRAW = 13
    CREATE_ORDER = 14
    CANCEL_ORDER = 15
    CANCEL_ALL_ORDERS = 16
    MODIFY_ORDER = 17
    MINT_SHARES = 18
    BURN_SHARES = 19
    UPDATE_LEVERAGE = 20
