"""Venue-neutral data types. Every exchange client converts its responses into these.

Prices are Decimal dollars in [0, 1]; sizes are Decimal contracts (both venues allow
fractional contracts down to 0.01).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

ONE = Decimal("1")


class Venue(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET_US = "polymarket_us"


class Side(str, Enum):
    YES = "yes"
    NO = "no"


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass
class OrderBook:
    """Normalized two-sided book for the YES outcome.

    yes_bids: best (highest) first.  yes_asks: best (lowest) first.
    The NO side is the mirror image: NO ask = 1 - YES bid, NO bid = 1 - YES ask.
    """

    venue: Venue
    market_id: str
    yes_bids: list[Level]
    yes_asks: list[Level]
    fetched_at: datetime
    exchange_ts: datetime | None = None
    depth: str = "full"          # "full" book, or "top" = best prices only (sizes unknown)

    @property
    def no_bids(self) -> list[Level]:
        return [Level(ONE - l.price, l.size) for l in self.yes_asks]

    @property
    def no_asks(self) -> list[Level]:
        return [Level(ONE - l.price, l.size) for l in self.yes_bids]

    def best_ask(self, side: Side) -> Level | None:
        levels = self.yes_asks if side is Side.YES else self.no_asks
        return levels[0] if levels else None

    def best_bid(self, side: Side) -> Level | None:
        levels = self.yes_bids if side is Side.YES else self.no_bids
        return levels[0] if levels else None

    def asks(self, side: Side) -> list[Level]:
        return self.yes_asks if side is Side.YES else self.no_asks

    def depth_at_or_better(self, side: Side, limit_price: Decimal) -> Decimal:
        """Contracts available to BUY `side` at prices <= limit_price."""
        return sum((l.size for l in self.asks(side) if l.price <= limit_price), Decimal(0))


@dataclass(frozen=True)
class Bracket:
    """Inclusive integer-degree range. None = unbounded on that side.

    Kalshi "less than 71" -> (None, 70); "between 71-72" -> (71, 72); "greater than 78" -> (79, None).
    """

    low: int | None
    high: int | None

    def contains(self, value: int) -> bool:
        return (self.low is None or value >= self.low) and (self.high is None or value <= self.high)

    def label(self) -> str:
        if self.low is None:
            return f"<={self.high}"
        if self.high is None:
            return f">={self.low}"
        return f"{self.low}-{self.high}"


@dataclass
class Market:
    venue: Venue
    market_id: str            # Kalshi ticker / Polymarket US market slug
    event_id: str             # Kalshi event ticker / Polymarket US event slug
    series_id: str | None
    title: str
    rules: str                # full resolution text, stored verbatim for matching review
    settlement_source: str
    status: str
    close_time: datetime | None
    bracket: Bracket | None = None
    event_key: str | None = None   # same real-world event across venues (set by discovery)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
