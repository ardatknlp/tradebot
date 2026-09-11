from dataclasses import dataclass, field, asdict
from typing import Optional
import time


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


@dataclass
class Signal:
    side: str            # LONG / SHORT
    price: float
    stop: float
    take_profit: float
    atr: float
    reason: str


@dataclass
class Position:
    symbol: str
    side: str            # LONG / SHORT
    qty: float
    entry: float
    stop: float
    take_profit: float
    atr: float
    leverage: int
    opened_at: float = field(default_factory=time.time)
    entry_fee: float = 0.0
    funding_paid: float = 0.0
    trail_active: bool = False
    best_price: float = 0.0
    stop_order_id: Optional[int] = None
    tp_order_id: Optional[int] = None
    initial_stop: float = 0.0
    partial_done: bool = False
    realized_partial: float = 0.0   # kısmi kapanıştan gelen brüt kâr
    partial_fee: float = 0.0
    entry_type: str = "market"       # market | limit
    bars_open: int = 0

    @property
    def direction(self):
        return 1 if self.side == "LONG" else -1

    @property
    def notional(self):
        return self.qty * self.entry

    def unrealized(self, price):
        return (price - self.entry) * self.qty * self.direction

    def to_dict(self, price=None):
        d = asdict(self)
        if price is not None:
            d["mark_price"] = price
            d["unrealized_pnl"] = self.unrealized(price)
            d["unrealized_pct"] = (price / self.entry - 1) * 100 * self.direction
        return d


@dataclass
class PendingOrder:
    symbol: str
    side: str
    qty: float
    limit_price: float
    stop: float
    take_profit: float
    atr: float
    leverage: int
    created_at: float
    expires_at: float
    order_id: Optional[int] = None
    reason: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class Trade:
    symbol: str
    side: str
    qty: float
    entry: float
    exit: float
    gross_pnl: float
    fees: float
    funding: float
    net_pnl: float
    reason: str
    opened_at: float
    closed_at: float
    mode: str = "demo"

    def to_dict(self):
        return asdict(self)
