"""Trade class hierarchy.

Design notes
------------
- `Trade` is the base class holding fields common to every asset class.
- Subclasses (EquityTrade, BondTrade, FxTrade, CommodityTrade) add
  asset-class-specific economics and override `notional()` where the
  convention differs (e.g. bonds quote price per 100 face).
- Lifecycle is event-driven: `apply_amendment()` / `cancel()` mutate
  state, bump `version`, and append to an in-object audit trail. The
  same events flow over the simulated feed, so the DB and the objects
  agree on history.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .enums import AssetClass, EventType, Side, TradeStatus

# Fields an amendment is allowed to touch. Anything else (counterparty,
# asset class, side) requires cancel/rebook in real desks -- we enforce
# the same rule here.
AMENDABLE_FIELDS = {"quantity", "price", "trader", "settlement_date"}


@dataclass
class TradeEvent:
    """One lifecycle event applied to a trade (audit-trail entry)."""
    event_type: EventType
    event_time: datetime
    version: int
    changes: dict[str, Any] = field(default_factory=dict)  # field -> new value


@dataclass
class Trade:
    trade_id: str
    client_id: str
    instrument_id: str
    side: Side
    quantity: float
    price: float
    trade_time: datetime
    trader: str
    asset_class: AssetClass = field(init=False)
    currency: str = "USD"
    settlement_date: str | None = None
    status: TradeStatus = TradeStatus.NEW
    version: int = 1
    events: list[TradeEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"{self.trade_id}: quantity must be positive")
        if self.price <= 0:
            raise ValueError(f"{self.trade_id}: price must be positive")
        self.events.append(
            TradeEvent(EventType.NEW, self.trade_time, self.version)
        )

    # ------------------------------------------------------------------
    # Economics
    # ------------------------------------------------------------------
    def notional(self) -> float:
        """Gross notional in trade currency. Overridden per asset class."""
        return self.quantity * self.price

    def signed_quantity(self) -> float:
        return self.quantity if self.side is Side.BUY else -self.quantity

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def is_live(self) -> bool:
        return self.status is not TradeStatus.CANCELLED

    def apply_amendment(
        self, changes: dict[str, Any], event_time: datetime | None = None
    ) -> None:
        if not self.is_live:
            raise ValueError(f"{self.trade_id}: cannot amend a cancelled trade")
        illegal = set(changes) - AMENDABLE_FIELDS
        if illegal:
            raise ValueError(
                f"{self.trade_id}: fields not amendable: {sorted(illegal)}"
            )
        if "quantity" in changes and changes["quantity"] <= 0:
            raise ValueError(f"{self.trade_id}: amended quantity must be positive")
        if "price" in changes and changes["price"] <= 0:
            raise ValueError(f"{self.trade_id}: amended price must be positive")

        for k, v in changes.items():
            setattr(self, k, v)
        self.version += 1
        self.status = TradeStatus.AMENDED
        self.events.append(
            TradeEvent(
                EventType.AMEND,
                event_time or datetime.now(timezone.utc),
                self.version,
                dict(changes),
            )
        )

    def cancel(self, event_time: datetime | None = None) -> None:
        if not self.is_live:
            raise ValueError(f"{self.trade_id}: already cancelled")
        self.version += 1
        self.status = TradeStatus.CANCELLED
        self.events.append(
            TradeEvent(
                EventType.CANCEL,
                event_time or datetime.now(timezone.utc),
                self.version,
            )
        )

    def __repr__(self) -> str:  # keep logs readable
        return (
            f"<{type(self).__name__} {self.trade_id} {self.side.value} "
            f"{self.quantity:g} {self.instrument_id} @ {self.price:g} "
            f"[{self.status.value} v{self.version}]>"
        )


@dataclass(repr=False)
class EquityTrade(Trade):
    exchange: str = "NYSE"

    def __post_init__(self) -> None:
        self.asset_class = AssetClass.EQUITY
        super().__post_init__()


@dataclass(repr=False)
class BondTrade(Trade):
    """Fixed income. `price` is clean price per 100 face, `quantity` is face."""
    coupon: float = 0.0
    maturity: str | None = None

    def __post_init__(self) -> None:
        self.asset_class = AssetClass.FIXED_INCOME
        super().__post_init__()

    def notional(self) -> float:
        return self.quantity * self.price / 100.0


@dataclass(repr=False)
class FxTrade(Trade):
    """Spot/forward FX. `quantity` is base-ccy amount, `price` is the rate.

    USD notional: for USD-base pairs (USDJPY) the USD leg IS the base
    quantity; for USD-quote pairs (EURUSD) it's qty * rate.
    """
    currency_pair: str = "EURUSD"

    def __post_init__(self) -> None:
        self.asset_class = AssetClass.FX
        super().__post_init__()

    def notional(self) -> float:
        if self.currency_pair.startswith("USD"):
            return self.quantity
        return self.quantity * self.price


@dataclass(repr=False)
class CommodityTrade(Trade):
    """Futures-style commodity trade: notional = qty * price * contract size."""
    contract_size: float = 1.0

    def __post_init__(self) -> None:
        self.asset_class = AssetClass.COMMODITY
        super().__post_init__()

    def notional(self) -> float:
        return self.quantity * self.price * self.contract_size


TRADE_CLASS_BY_ASSET: dict[AssetClass, type[Trade]] = {
    AssetClass.EQUITY: EquityTrade,
    AssetClass.FIXED_INCOME: BondTrade,
    AssetClass.FX: FxTrade,
    AssetClass.COMMODITY: CommodityTrade,
}
