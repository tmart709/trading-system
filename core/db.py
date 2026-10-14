"""SQLite access layer. Keep SQL portable (see db/schema.sql) so moving to Postgres is a driver swap."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.models import Market, OrderBook

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _levels(levels) -> str:
    return json.dumps([[str(l.price), str(l.size)] for l in levels])


class Database:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)  # autocommit; explicit txns below
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate_before_schema()
        self.conn.executescript(SCHEMA.read_text())
        self._migrate()

    def _migrate_before_schema(self) -> None:
        """Tables whose primary key changed: drop so the schema script recreates them.
        Only rebuildable data lives here (`cli.py calibrate` refills it)."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(calibration_samples)")}
        if have and "variable" not in have:
            self.conn.execute("DROP TABLE calibration_samples")

    def _migrate(self) -> None:
        """Add columns introduced after a DB was created (CREATE TABLE IF NOT EXISTS won't)."""
        added = {"markets": [("settled_at", "TEXT"), ("event_key", "TEXT")],
                 "forecasts": [("variable", "TEXT NOT NULL DEFAULT 'high'")],
                 "cli_actuals": [("low_f", "INTEGER")]}
        for table, cols in added.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")

    def close(self) -> None:
        self.conn.close()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    # ---- markets ----
    def upsert_markets(self, markets: list[Market]) -> int:
        """Insert/update markets. Returns how many had new or changed rules."""
        now, changed = utcnow(), 0
        with self.conn:
            for m in markets:
                prev = self.conn.execute(
                    "SELECT rules, settlement_source FROM markets WHERE venue=? AND market_id=?",
                    (m.venue.value, m.market_id),
                ).fetchone()
                if prev is None or prev["rules"] != m.rules or prev["settlement_source"] != m.settlement_source:
                    changed += 1
                    self.conn.execute(
                        "INSERT INTO market_rules_history (venue, market_id, rules, settlement_source, seen_at)"
                        " VALUES (?,?,?,?,?)",
                        (m.venue.value, m.market_id, m.rules, m.settlement_source, now),
                    )
                result = m.raw.get("result") or None
                self.conn.execute(
                    """INSERT INTO markets (venue, market_id, event_id, series_id, title, rules, settlement_source,
                           status, close_time, bracket_low, bracket_high, result, raw_json, first_seen, last_seen,
                           event_key)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT (venue, market_id) DO UPDATE SET
                           event_id=excluded.event_id, series_id=excluded.series_id, title=excluded.title,
                           event_key=COALESCE(excluded.event_key, markets.event_key),
                           rules=excluded.rules, settlement_source=excluded.settlement_source,
                           status=excluded.status, close_time=excluded.close_time,
                           bracket_low=excluded.bracket_low, bracket_high=excluded.bracket_high,
                           result=COALESCE(excluded.result, markets.result),
                           raw_json=excluded.raw_json, last_seen=excluded.last_seen""",
                    (
                        m.venue.value, m.market_id, m.event_id, m.series_id, m.title, m.rules,
                        m.settlement_source, m.status, _iso(m.close_time),
                        m.bracket.low if m.bracket else None, m.bracket.high if m.bracket else None,
                        result, json.dumps(m.raw, default=str), now, now, m.event_key,
                    ),
                )
        return changed

    # ---- order books ----
    def insert_snapshots(self, books: Iterable[OrderBook]) -> int:
        rows = []
        for b in books:
            bb = b.yes_bids[0] if b.yes_bids else None
            ba = b.yes_asks[0] if b.yes_asks else None
            sized = b.depth == "full"
            rows.append((
                b.venue.value, b.market_id, _iso(b.fetched_at), _iso(b.exchange_ts), b.depth,
                str(bb.price) if bb else None, str(bb.size) if bb and sized else None,
                str(ba.price) if ba else None, str(ba.size) if ba and sized else None,
                _levels(b.yes_bids) if sized else "[]", _levels(b.yes_asks) if sized else "[]",
            ))
        with self.conn:
            self.conn.executemany(
                """INSERT INTO orderbook_snapshots (venue, market_id, ts, exchange_ts, depth, best_yes_bid,
                       best_yes_bid_size, best_yes_ask, best_yes_ask_size, yes_bids_json, yes_asks_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return len(rows)

    # ---- forecasts ----
    def insert_forecasts(self, rows: list[dict]) -> int:
        now = utcnow()
        with self.conn:
            self.conn.executemany(
                """INSERT INTO forecasts (fetched_at, city, source, model, variable, target_date, issued_at,
                       point_f, members_json, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [
                    (now, r["city"], r["source"], r["model"], r.get("variable", "high"), r["target_date"], r.get("issued_at"),
                     r.get("point_f"), json.dumps(r["members"]) if r.get("members") is not None else None,
                     json.dumps(r["raw"], default=str) if r.get("raw") is not None else None)
                    for r in rows
                ],
            )
        return len(rows)

    def insert_observations(self, rows: list[dict]) -> int:
        now = utcnow()
        with self.conn:
            cur = self.conn.executemany(
                """INSERT INTO observations (fetched_at, city, station, obs_time, temp_f, raw_json)
                   VALUES (?,?,?,?,?,?) ON CONFLICT (station, obs_time) DO NOTHING""",
                [(now, r["city"], r["station"], r["obs_time"], r["temp_f"], json.dumps(r.get("raw"))) for r in rows],
            )
        return cur.rowcount

    # ---- cross-venue pairs ----
    def propose_pair(self, k: Market, p: Market, reason: str, warnings: list[str]) -> bool:
        """Insert a pending pair if new. Returns True if inserted. Never changes an existing pair's status."""
        cur = self.conn.execute(
            """INSERT INTO market_pairs (kalshi_market_id, pm_market_id, match_reason, warnings,
                   kalshi_rules, pm_rules, kalshi_source, pm_source, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,'pending',?)
               ON CONFLICT (kalshi_market_id, pm_market_id) DO NOTHING""",
            (k.market_id, p.market_id, reason, "\n".join(warnings), k.rules, p.rules,
             k.settlement_source, p.settlement_source, utcnow()),
        )
        return cur.rowcount == 1

    def set_pair_status(self, pair_id: int, status: str, notes: str | None = None) -> None:
        if status not in ("approved", "rejected", "pending"):
            raise ValueError(status)
        self.conn.execute(
            "UPDATE market_pairs SET status=?, notes=COALESCE(?, notes), reviewed_at=? WHERE id=?",
            (status, notes, utcnow(), pair_id),
        )

    def approved_pairs(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM market_pairs WHERE status='approved'")

    # ---- ops ----
    def log_event(self, level: str, component: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO system_events (ts, level, component, message) VALUES (?,?,?,?)",
            (utcnow(), level, component, message),
        )
