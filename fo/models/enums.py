"""Shared enums for the front-office domain model."""
from enum import Enum


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    FIXED_INCOME = "FIXED_INCOME"
    FX = "FX"
    COMMODITY = "COMMODITY"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(str, Enum):
    """Lifecycle status of a trade.

    NEW      -> booked, live
    AMENDED  -> live, at least one amendment applied
    CANCELLED-> dead, excluded from positions/P&L
    """
    NEW = "NEW"
    AMENDED = "AMENDED"
    CANCELLED = "CANCELLED"


class EventType(str, Enum):
    """Type of a trade lifecycle event as it appears on the feed."""
    NEW = "NEW"
    AMEND = "AMEND"
    CANCEL = "CANCEL"


class ClientTier(str, Enum):
    PLATINUM = "PLATINUM"
    GOLD = "GOLD"
    SILVER = "SILVER"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
