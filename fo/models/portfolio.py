"""Client, Order, Position and Portfolio.

Position keeping uses weighted-average-cost (WAC):
- Same-direction fills move the average cost.
- Opposite-direction fills realize P&L against average cost and can
  flip the position through zero (the flip re-opens at the fill price).
Cancels are handled by replaying a reversing fill, so a cancelled trade
leaves positions exactly as if it never happened.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .enums import AssetClass, ClientTier, OrderStatus, Side
from .trade import Trade


@dataclass
class Client:
    client_id: str
    name: str
    tier: ClientTier
    sector: str
    region: str
    onboarded: str  # ISO date

    def __repr__(self) -> str:
        return f"<Client {self.client_id} {self.name} [{self.tier.value}]>"


@dataclass
class Order:
    """A client order; trades are fills against it."""
    order_id: str
    client_id: str
    instrument_id: str
    side: Side
    quantity: float
    status: OrderStatus = OrderStatus.OPEN
    filled_quantity: float = 0.0

    def record_fill(self, qty: float) -> None:
        if qty <= 0:
            raise ValueError(f"{self.order_id}: fill qty must be positive")
        if self.filled_quantity + qty > self.quantity + 1e-9:
            raise ValueError(f"{self.order_id}: fill exceeds order quantity")
        self.filled_quantity += qty
        self.status = (
            OrderStatus.FILLED
            if abs(self.filled_quantity - self.quantity) < 1e-9
            else OrderStatus.PARTIALLY_FILLED
        )


@dataclass
class Position:
    instrument_id: str
    asset_class: AssetClass
    net_quantity: float = 0.0
    avg_cost: float = 0.0
    realized_pnl: float = 0.0

    def apply_fill(self, signed_qty: float, price: float) -> None:
        """Update position for a signed fill quantity at `price`."""
        if signed_qty == 0:
            return
        same_direction = self.net_quantity == 0 or (
            (self.net_quantity > 0) == (signed_qty > 0)
        )
        if same_direction:
            total = self.net_quantity + signed_qty
            self.avg_cost = (
                (abs(self.net_quantity) * self.avg_cost + abs(signed_qty) * price)
                / abs(total)
            )
            self.net_quantity = total
            return

        # Opposite direction: close against average cost.
        closing = min(abs(signed_qty), abs(self.net_quantity))
        direction = 1.0 if self.net_quantity > 0 else -1.0
        self.realized_pnl += closing * (price - self.avg_cost) * direction
        self.net_quantity += signed_qty
        if abs(self.net_quantity) < 1e-9:
            self.net_quantity = 0.0
            self.avg_cost = 0.0
        elif (self.net_quantity > 0) != (direction > 0):
            # Flipped through zero -- remainder opens at fill price.
            self.avg_cost = price

    def unrealized_pnl(self, mark: float) -> float:
        return self.net_quantity * (mark - self.avg_cost)


@dataclass
class Portfolio:
    """Per-client book: live trades and derived positions."""
    client: Client
    trades: dict[str, Trade] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)

    def book(self, trade: Trade) -> None:
        if trade.trade_id in self.trades:
            raise ValueError(f"duplicate trade_id {trade.trade_id}")
        if trade.client_id != self.client.client_id:
            raise ValueError(
                f"{trade.trade_id}: client mismatch "
                f"({trade.client_id} != {self.client.client_id})"
            )
        self.trades[trade.trade_id] = trade
        self._position_for(trade).apply_fill(trade.signed_quantity(), trade.price)

    def amend(self, trade_id: str, changes: dict) -> None:
        trade = self._get(trade_id)
        # Reverse old economics, apply amendment, replay new economics.
        pos = self._position_for(trade)
        pos.apply_fill(-trade.signed_quantity(), trade.price)
        trade.apply_amendment(changes)
        pos.apply_fill(trade.signed_quantity(), trade.price)

    def cancel(self, trade_id: str) -> None:
        trade = self._get(trade_id)
        self._position_for(trade).apply_fill(
            -trade.signed_quantity(), trade.price
        )
        trade.cancel()

    # ------------------------------------------------------------------
    def gross_notional(self) -> float:
        return sum(t.notional() for t in self.trades.values() if t.is_live)

    def realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self.positions.values())

    def _position_for(self, trade: Trade) -> Position:
        return self.positions.setdefault(
            trade.instrument_id,
            Position(trade.instrument_id, trade.asset_class),
        )

    def _get(self, trade_id: str) -> Trade:
        try:
            return self.trades[trade_id]
        except KeyError:
            raise KeyError(
                f"{trade_id} not found in portfolio {self.client.client_id}"
            ) from None
