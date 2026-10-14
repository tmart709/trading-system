"""Mandatory pre-trade risk checks + kill switch + daily loss halt. Applies to paper exactly as to live.

Every check returns a reason string on failure; an order goes out only if the list is empty.
The kill switch and halts live in the DB `controls` table so they survive restarts and can be set
from the CLI while the engine is running.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from core.db import Database, utcnow
from core.execution.portfolio import Exposure, Position, exposure, new_cost_since, positions, realized_between
from core.marketdata import MarketData
from core.models import Side, Venue

log = logging.getLogger("risk")

KILL_KEY = "kill_switch"


@dataclass(frozen=True)
class ProposedOrder:
    strategy: str
    venue: str
    market_id: str
    event_id: str
    side: str                  # yes | no
    contracts: Decimal
    limit_price: Decimal
    cost: Decimal              # contracts * vwap + fee
    close_time: datetime | None


def day_bounds(now: datetime, tz: str) -> tuple[datetime, datetime]:
    local = now.astimezone(ZoneInfo(tz))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


class RiskManager:
    def __init__(self, risk_cfg: dict, db: Database, tz: str = "America/New_York"):
        self.cfg = risk_cfg
        self.db = db
        self.tz = tz

    # ---------------- controls ----------------
    def _control(self, key: str) -> str | None:
        rows = self.db.query("SELECT value FROM controls WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def _set_control(self, key: str, value: str, reason: str) -> None:
        self.db.conn.execute(
            "INSERT INTO controls (key, value, updated_at, reason) VALUES (?,?,?,?) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, reason=excluded.reason",
            (key, value, utcnow(), reason))

    def kill_switch_on(self) -> bool:
        return self._control(KILL_KEY) == "on"

    def set_kill_switch(self, on: bool, reason: str) -> None:
        self._set_control(KILL_KEY, "on" if on else "off", reason)
        self.db.log_event("CRITICAL" if on else "INFO", "risk", f"kill switch {'ON' if on else 'off'}: {reason}")

    def _halt_key(self, now: datetime) -> str:
        return "halt_" + now.astimezone(ZoneInfo(self.tz)).date().isoformat()

    def halted_today(self, now: datetime) -> bool:
        return self._control(self._halt_key(now)) == "on"

    def halt_today(self, now: datetime, reason: str) -> None:
        self._set_control(self._halt_key(now), "on", reason)
        self.db.log_event("CRITICAL", "risk", f"trading halted for the day: {reason}")

    # ---------------- PnL ----------------
    def day_pnl(self, mode: str, md: MarketData, now: datetime) -> tuple[Decimal, Decimal]:
        """(realized today, unrealized on all open positions marked to best bid). Conservative:
        unrealized losses from earlier days count against today."""
        start, end = day_bounds(now, self.tz)
        realized = sum((p.realized_pnl for p in realized_between(self.db, mode, start, end)), Decimal(0))
        unrealized = Decimal(0)
        for p in positions(self.db, mode):
            if p.settled:
                continue
            got = md.get(Venue(p.venue), p.market_id)
            if not got:
                continue   # no book: treat as flat at cost (unknown)
            bid = got[0].best_bid(Side(p.side))
            unrealized += (p.contracts * bid.price if bid else Decimal(0)) - p.cost
        return realized, unrealized

    def uncapped(self, mode: str) -> bool:
        """Paper-only switch that disables exposure caps and the daily loss halt (sample building)."""
        return mode == "paper" and bool(self.cfg.get("paper_uncapped"))

    def enforce_daily_loss(self, mode: str, md: MarketData, now: datetime) -> str | None:
        """Halts the day if the loss limit is breached. Returns the reason if it halted just now."""
        if self.uncapped(mode) or self.halted_today(now):
            return None
        realized, unrealized = self.day_pnl(mode, md, now)
        limit = Decimal(str(self.cfg["daily_loss_limit_usd"]))
        if realized + unrealized <= -limit:
            reason = f"daily loss {realized + unrealized:.2f} (realized {realized:.2f}, MTM {unrealized:.2f}) hit limit -{limit}"
            self.halt_today(now, reason)
            return reason
        return None

    # ---------------- pre-trade ----------------
    def check_order(
        self,
        o: ProposedOrder,
        *,
        mode: str,
        now: datetime,
        book_age_s: float,
        forecast_age_min: float,
        min_minutes_to_close: float,
        current: list[Position] | None = None,
    ) -> list[str]:
        c = self.cfg
        reasons: list[str] = []
        D = lambda k: Decimal(str(c[k]))  # noqa: E731

        if self.kill_switch_on():
            reasons.append("kill switch is on")
        if self.halted_today(now) and not self.uncapped(mode):
            reasons.append("trading halted for today (daily loss limit)")

        # data freshness
        if book_age_s > c["max_book_age_seconds"]:
            reasons.append(f"order book is {book_age_s:.0f}s old (> {c['max_book_age_seconds']}s)")
        if forecast_age_min > c["max_forecast_age_minutes"]:
            reasons.append(f"forecast is {forecast_age_min:.0f}min old (> {c['max_forecast_age_minutes']}min)")

        # order sanity
        if o.contracts <= 0:
            reasons.append("non-positive size")
        if o.contracts > D("max_contracts_per_order"):
            reasons.append(f"size {o.contracts} > max {c['max_contracts_per_order']} per order")
        if not (D("min_price") <= o.limit_price <= D("max_price")):
            reasons.append(f"limit price {o.limit_price} outside [{c['min_price']}, {c['max_price']}]")
        if o.contracts > 0 and o.cost / o.contracts >= 1:
            reasons.append("cost per contract >= $1 (can't profit)")
        if o.side not in ("yes", "no"):
            reasons.append(f"bad side {o.side!r}")
        if o.close_time is None:
            reasons.append("unknown market close time")
        elif (o.close_time - now).total_seconds() < min_minutes_to_close * 60:
            reasons.append(f"market closes in < {min_minutes_to_close} min")

        # exposure limits
        current = positions(self.db, mode) if current is None else current
        ex: Exposure = exposure(current)
        held = ex.sides.get((o.venue, o.market_id), set())
        if held and o.side not in held:
            reasons.append(f"already hold the opposite side ({', '.join(held)}) in this market")
        if self.uncapped(mode):
            return reasons
        checks = [
            (ex.by_market.get((o.venue, o.market_id), Decimal(0)), "max_position_per_market_usd", "market"),
            (ex.by_event.get(o.event_id, Decimal(0)), "max_exposure_per_event_usd", "event"),
            (ex.by_venue.get(o.venue, Decimal(0)), "max_exposure_per_venue_usd", f"venue {o.venue}"),
            (ex.total, "max_total_exposure_usd", "total"),
        ]
        for have, key, label in checks:
            if have + o.cost > D(key):
                reasons.append(f"{label} exposure {have:.2f} + {o.cost:.2f} > {c[key]}")
        start, _ = day_bounds(now, self.tz)
        today = new_cost_since(self.db, mode, start)
        if today + o.cost > D("max_new_exposure_per_day_usd"):
            reasons.append(f"new exposure today {today:.2f} + {o.cost:.2f} > {c['max_new_exposure_per_day_usd']}")
        return reasons

    def headroom(self, o_venue: str, o_market: str, o_event: str, mode: str, now: datetime,
                 current: list[Position] | None = None) -> Decimal:
        """Largest cost an order could have right now without breaking an exposure cap."""
        if self.uncapped(mode):
            return Decimal(10) ** 9
        c = self.cfg
        D = lambda k: Decimal(str(c[k]))  # noqa: E731
        current = positions(self.db, mode) if current is None else current
        ex = exposure(current)
        start, _ = day_bounds(now, self.tz)
        rooms = [
            D("max_position_per_market_usd") - ex.by_market.get((o_venue, o_market), Decimal(0)),
            D("max_exposure_per_event_usd") - ex.by_event.get(o_event, Decimal(0)),
            D("max_exposure_per_venue_usd") - ex.by_venue.get(o_venue, Decimal(0)),
            D("max_total_exposure_usd") - ex.total,
            D("max_new_exposure_per_day_usd") - new_cost_since(self.db, mode, start),
        ]
        return max(Decimal(0), min(rooms))
