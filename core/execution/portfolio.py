"""Positions, exposure and PnL, derived from the fills table (the single source of truth).

Every fill is a BUY of an outcome (YES or NO) for `contracts` at `price` plus `fee`. A position pays
$1/contract if its side matches the market result. Exposure = cost basis of unsettled positions.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.db import Database


@dataclass
class Position:
    strategy: str
    venue: str
    market_id: str
    event_id: str              # venue-neutral event key when known (so caps span venues)
    side: str
    contracts: Decimal
    cost: Decimal              # sum(contracts * price + fee)
    result: str | None         # market result: yes / no / None (unsettled)

    @property
    def settled(self) -> bool:
        return self.result in ("yes", "no")

    @property
    def payout(self) -> Decimal:
        return self.contracts if self.result == self.side else Decimal(0)

    @property
    def realized_pnl(self) -> Decimal:
        return self.payout - self.cost if self.settled else Decimal(0)


def positions(db: Database, mode: str, strategy: str | None = None) -> list[Position]:
    rows = db.query(
        """SELECT o.strategy, f.venue, f.market_id, f.side, f.contracts, f.price, f.fee,
                  COALESCE(m.event_key, m.event_id) AS event_id, m.result
           FROM fills f JOIN orders o ON o.id = f.order_id
           LEFT JOIN markets m ON m.venue = f.venue AND m.market_id = f.market_id
           WHERE f.mode = ? AND (? IS NULL OR o.strategy = ?)""",
        (mode, strategy, strategy))
    agg: dict[tuple, Position] = {}
    for r in rows:
        key = (r["strategy"], r["venue"], r["market_id"], r["side"])
        p = agg.get(key)
        if p is None:
            p = agg[key] = Position(r["strategy"], r["venue"], r["market_id"], r["event_id"] or "",
                                    r["side"], Decimal(0), Decimal(0), r["result"])
        c = Decimal(r["contracts"])
        p.contracts += c
        p.cost += c * Decimal(r["price"]) + Decimal(r["fee"])
    return list(agg.values())


@dataclass
class Exposure:
    by_market: dict[tuple[str, str], Decimal]
    by_event: dict[str, Decimal]
    by_venue: dict[str, Decimal]
    total: Decimal
    sides: dict[tuple[str, str], set[str]]   # (venue, market) -> sides held


def exposure(open_positions: list[Position]) -> Exposure:
    by_market, by_event, by_venue = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
    sides: dict[tuple[str, str], set[str]] = defaultdict(set)
    total = Decimal(0)
    for p in open_positions:
        if p.settled:
            continue
        by_market[(p.venue, p.market_id)] += p.cost
        by_event[p.event_id] += p.cost
        by_venue[p.venue] += p.cost
        sides[(p.venue, p.market_id)].add(p.side)
        total += p.cost
    return Exposure(dict(by_market), dict(by_event), dict(by_venue), total, dict(sides))


def new_cost_since(db: Database, mode: str, since_utc: datetime, strategy: str | None = None) -> Decimal:
    rows = db.query(
        """SELECT f.contracts, f.price, f.fee FROM fills f JOIN orders o ON o.id = f.order_id
           WHERE f.mode=? AND f.ts >= ? AND (? IS NULL OR o.strategy = ?)""",
        (mode, since_utc.isoformat(), strategy, strategy))
    return sum((Decimal(r["contracts"]) * Decimal(r["price"]) + Decimal(r["fee"]) for r in rows), Decimal(0))


def realized_between(db: Database, mode: str, start_utc: datetime, end_utc: datetime) -> list[Position]:
    """Positions whose market settled within [start, end)."""
    settled = {(r["venue"], r["market_id"]) for r in db.query(
        "SELECT venue, market_id FROM markets WHERE settled_at >= ? AND settled_at < ?",
        (start_utc.isoformat(), end_utc.isoformat()))}
    return [p for p in positions(db, mode) if (p.venue, p.market_id) in settled and p.settled]
