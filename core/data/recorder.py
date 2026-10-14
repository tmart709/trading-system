"""Record mode: market metadata + rules, order book snapshots, forecasts, observations, settlements.

Each job runs on its own interval and is isolated: an exception in one job is logged/alerted and the
loop continues. Nothing here can place an order.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from core.alerts.notifier import Notifier
from core.config import CityConfig, Config
from core.data.forecasts import WeatherSources
from core.db import Database, utcnow
from core.exchanges.kalshi import KalshiClient
from core.exchanges.polymarket_us import PolymarketUSClient
from core.exchanges.polymarket_us_ws import PolymarketMarketStream
from core.marketdata import MarketData
from core.matching.weather import kalshi_event_date, match_weather_markets, pm_event_date
from core.models import Market, Venue

log = logging.getLogger("recorder")

ALERT_AFTER_CONSECUTIVE_FAILURES = 3


class Recorder:
    def __init__(self, cfg: Config, db: Database, kalshi: KalshiClient, pm: PolymarketUSClient,
                 weather: WeatherSources, notifier: Notifier, md: MarketData | None = None,
                 pm_stream: PolymarketMarketStream | None = None):
        self.cfg, self.db, self.kalshi, self.pm, self.weather, self.notifier = cfg, db, kalshi, pm, weather, notifier
        self.cities: list[CityConfig] = list(cfg.cities.values())
        self.tracked: dict[Venue, list[str]] = {Venue.KALSHI: [], Venue.POLYMARKET_US: []}
        self._kalshi_series: dict[str, dict] = {}
        self._failures: dict[str, int] = {}
        self.pm_stream = pm_stream
        self.md = md or MarketData(pm_stream)
        # Async callbacks run right after each successful book snapshot (strategies hook in here).
        self.after_snapshot: list[tuple[str, Callable[[], Awaitable[dict]]]] = []
        # Extra periodic jobs: name -> (interval_seconds, coroutine fn)
        self.extra_jobs: dict[str, tuple[float, Callable[[], Awaitable[dict]]]] = {}

    def kalshi_fee_params(self, series_ticker: str) -> dict:
        s = self._kalshi_series.get(series_ticker) or {}
        return {"fee_type": s.get("fee_type", "quadratic"), "fee_multiplier": s.get("fee_multiplier", 1)}

    # ---------------- jobs ----------------
    async def refresh_markets(self) -> dict:
        """List open weather markets on both venues, store rules, propose cross-venue pairs."""
        stats = {"kalshi_markets": 0, "pm_markets": 0, "rules_changed": 0, "pairs_proposed": 0}
        kalshi_ids, pm_ids = [], []

        pm_events = await self.pm.list_events(categories=["climate"], active=True, closed=False)
        for city in self.cities:
            k_markets: list[Market] = []        # daily highs (pairable with Polymarket US)
            k_lows: list[Market] = []
            for variable in ("high", "low"):
                ticker = city.kalshi_series_for(variable)
                if not ticker:
                    continue
                series = await self.kalshi.get_series(ticker)
                self._kalshi_series[ticker] = series
                got = await self.kalshi.list_markets(series_ticker=ticker, series=series)
                for m in got:
                    d = kalshi_event_date(m.event_id)
                    m.event_key = f"weather:{city.code}:{variable}:{d.isoformat()}" if d else None
                (k_markets if variable == "high" else k_lows).extend(got)
            p_markets: list[Market] = []
            if city.polymarket_event_prefix:
                prefix = city.polymarket_event_prefix + "-"
                for ev in pm_events:
                    if ev.get("slug", "").startswith(prefix):
                        p_markets += [PolymarketUSClient.to_market(m, ev) for m in ev.get("markets", [])
                                      if not m.get("closed")]
            for m in p_markets:
                d = pm_event_date(m.event_id)
                m.event_key = f"weather:{city.code}:high:{d.isoformat()}" if d else None
            stats["rules_changed"] += self.db.upsert_markets(k_markets + k_lows + p_markets)
            stats["kalshi_markets"] += len(k_markets) + len(k_lows)
            stats["pm_markets"] += len(p_markets)
            kalshi_ids += [m.market_id for m in k_markets + k_lows]
            pm_ids += [m.market_id for m in p_markets]

            for k, p, reason, warnings in match_weather_markets(city, k_markets, p_markets):
                if self.db.propose_pair(k, p, reason, warnings):
                    stats["pairs_proposed"] += 1

            for m in k_markets + k_lows + p_markets:
                if m.bracket is None:
                    log.warning("could not parse bracket for %s %s", m.venue.value, m.market_id)

        self.tracked = {Venue.KALSHI: kalshi_ids, Venue.POLYMARKET_US: pm_ids}
        if self.pm_stream:
            self.pm_stream.set_markets(pm_ids)
        return stats

    async def snapshot_books(self) -> dict:
        """Kalshi: full depth for every market in one batch call.
        Polymarket US: full books from the live WebSocket when it's healthy; otherwise best bid/ask from
        one listing call (plus the slow REST rotation in `pm_book_rotator` for full depth)."""
        k_books = await self.kalshi.get_orderbooks(self.tracked[Venue.KALSHI]) if self.tracked[Venue.KALSHI] else {}
        for b in k_books.values():
            self.md.put(b)
        pm_books, source = [], "none"
        wanted = self.tracked[Venue.POLYMARKET_US]
        now = datetime.now(timezone.utc)
        if wanted and self.pm_stream and self.pm_stream.healthy():
            source = "websocket"
            # Stream books are current as of now (connection alive); record them at the snapshot time.
            pm_books = [dataclasses.replace(self.pm_stream.books[s], fetched_at=now)
                        for s in wanted if s in self.pm_stream.books]
        elif wanted:
            source = "rest_top_of_book"
            for ev in await self.pm.list_events(categories=["climate"], active=True, closed=False):
                pm_books += [PolymarketUSClient.top_of_book(m, now) for m in ev.get("markets", []) if m["slug"] in wanted]
        n = self.db.insert_snapshots(list(k_books.values()) + pm_books)
        return {"kalshi_full_books": len(k_books), "pm_books": len(pm_books), "pm_source": source, "rows": n}

    async def pm_book_rotator(self, stop: asyncio.Event) -> None:
        """Continuously cycle through tracked Polymarket US markets fetching full books at the
        client's book rate limit. Each market gets a full-depth snapshot every N * (1/rate) seconds."""
        i = 0
        while not stop.is_set():
            slugs = self.tracked[Venue.POLYMARKET_US]
            if not slugs or (self.pm_stream and self.pm_stream.healthy()):
                await asyncio.sleep(5)   # nothing to do, or the WebSocket already provides full books
                continue
            slug = slugs[i % len(slugs)]
            i += 1
            try:
                book = await self.pm.get_orderbook(slug)
                self.md.put(book)
                self.db.insert_snapshots([book])
                self._failures["pm_book_rotator"] = 0
            except Exception as e:
                n = self._failures.get("pm_book_rotator", 0) + 1
                self._failures["pm_book_rotator"] = n
                log.warning("polymarket_us full book %s failed: %s", slug, e)
                if n == 10:
                    await self.notifier.send("error", f"polymarket_us book fetches failing repeatedly: {e}")
                await asyncio.sleep(5)

    async def record_weather(self) -> dict:
        fc_rows = obs_rows = 0
        for city in self.cities:
            rows = []
            try:
                rows += await self.weather.nws_forecasts(city)
            except Exception as e:
                log.warning("NWS forecast failed for %s: %s", city.code, e)
            rows += await self.weather.ensemble_forecasts(city)
            rows += await self.weather.deterministic_forecasts(city)
            fc_rows += self.db.insert_forecasts(rows)
            try:
                obs_rows += self.db.insert_observations(await self.weather.observations(city))
            except Exception as e:
                log.warning("NWS observations failed for %s: %s", city.code, e)
        return {"forecast_rows": fc_rows, "new_observations": obs_rows}

    async def sweep_settlements(self, pm_limit: int = 80) -> dict:
        """Fill in results for closed markets (needed for P&L, calibration and backtests).

        Kalshi: one bulk query per series for markets settled since the oldest one we're waiting on.
        Polymarket US: per-market settlement lookups (its rate-limited endpoint), oldest first.
        """
        now = datetime.now(timezone.utc)
        due = self.db.query(
            "SELECT venue, market_id, series_id, close_time FROM markets WHERE result IS NULL AND close_time < ?",
            (now.isoformat(),))
        k_due = [r for r in due if r["venue"] == Venue.KALSHI.value]
        settled = 0
        by_series: dict[str, list] = {}
        for r in k_due:
            by_series.setdefault(r["series_id"], []).append(r)
        for series, rows in by_series.items():
            oldest = min(datetime.fromisoformat(r["close_time"]) for r in rows)
            min_ts = int((oldest - timedelta(days=1)).timestamp())
            results = {}
            for m in await self.kalshi._paginate("/markets", "markets",
                                                 {"series_ticker": series, "status": "settled", "min_close_ts": min_ts}):
                if m.get("result"):
                    results[m["ticker"]] = (m["result"], m.get("status", "settled"))
            with self.db.conn:
                for r in rows:
                    if r["market_id"] in results:
                        res, status = results[r["market_id"]]
                        self.db.conn.execute(
                            "UPDATE markets SET result=?, status=?, settled_at=? WHERE venue=? AND market_id=?",
                            (res, status, utcnow(), r["venue"], r["market_id"]))
                        settled += 1
        # Polymarket US settles ~8am ET the morning after (11am if the CLI needs review); markets close
        # at their endDate, so anything past close is worth asking about.
        pm_due = sorted((r for r in due if r["venue"] == Venue.POLYMARKET_US.value), key=lambda r: r["close_time"])
        for r in pm_due[:pm_limit]:
            px = await self.pm.get_settlement(r["market_id"])
            if px is not None:
                self.db.conn.execute(
                    "UPDATE markets SET result=?, status='settled', settled_at=? WHERE venue=? AND market_id=?",
                    ("yes" if px == 1 else "no" if px == 0 else str(px), utcnow(), r["venue"], r["market_id"]))
                settled += 1
        return {"kalshi_waiting": len(k_due), "pm_waiting": len(pm_due), "settled": settled}

    # ---------------- loop ----------------
    async def _run_job(self, name: str, coro) -> dict | None:
        t0 = time.monotonic()
        try:
            out = await coro
            self._failures[name] = 0
            log.info("%s ok in %.1fs %s", name, time.monotonic() - t0, out)
            return out
        except Exception as e:
            n = self._failures.get(name, 0) + 1
            self._failures[name] = n
            msg = f"recorder job {name} failed ({n}x): {type(e).__name__}: {e}"
            log.exception(msg)
            self.db.log_event("ERROR", "recorder", msg)
            if n == ALERT_AFTER_CONSECUTIVE_FAILURES:
                await self.notifier.send("error", msg)
            return None

    async def run(self, once: bool = False, stop: asyncio.Event | None = None) -> None:
        rc = self.cfg.recorder
        intervals = {
            "refresh_markets": rc["market_refresh_interval_seconds"],
            "snapshot_books": rc["snapshot_interval_seconds"],
            "record_weather": rc["forecast_interval_seconds"],
            "sweep_settlements": rc["market_refresh_interval_seconds"],
        }
        jobs = {
            "refresh_markets": self.refresh_markets,
            "record_weather": self.record_weather,      # before books, so strategies see forecasts on tick 1
            "snapshot_books": self.snapshot_books,
            "sweep_settlements": self.sweep_settlements,
        }
        for name, (interval, fn) in self.extra_jobs.items():
            intervals[name], jobs[name] = interval, fn
        last: dict[str, float] = {}
        stop = stop or asyncio.Event()
        self.db.log_event("INFO", "recorder", "started")
        rotator = None
        stream_task = asyncio.create_task(self.pm_stream.run(stop)) if (self.pm_stream and not once) else None
        if stream_task:
            await self._wait_for_stream(stop)
            last["refresh_markets"] = time.monotonic()
        while not stop.is_set():
            for name, fn in jobs.items():  # order matters: markets must be listed before books
                if time.monotonic() - last.get(name, -1e9) >= intervals[name]:
                    ok = await self._run_job(name, fn()) is not None
                    # on failure, retry after at most 60s instead of a full interval
                    last[name] = time.monotonic() - (0 if ok else max(0, intervals[name] - 60))
                    if name == "snapshot_books" and ok:
                        for hook_name, hook in self.after_snapshot:
                            await self._run_job(hook_name, hook())
            if once:
                await self._one_pass_full_books()
                return
            if rotator is None:
                rotator = asyncio.create_task(self.pm_book_rotator(stop))
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        for t in (rotator, stream_task):
            if t:
                t.cancel()
        self.db.log_event("INFO", "recorder", "stopped")

    async def _wait_for_stream(self, stop: asyncio.Event, timeout: float = 15.0) -> None:
        """On startup, list markets and give the WebSocket a moment to deliver initial books."""
        await self._run_job("refresh_markets", self.refresh_markets())
        t0 = time.monotonic()
        while not stop.is_set() and time.monotonic() - t0 < timeout:
            if self.pm_stream.healthy() and len(self.pm_stream.books) >= len(self.tracked[Venue.POLYMARKET_US]):
                break
            await asyncio.sleep(0.5)
        log.info("polymarket_us ws: %d/%d books after %.1fs", len(self.pm_stream.books),
                 len(self.tracked[Venue.POLYMARKET_US]), time.monotonic() - t0)

    async def _one_pass_full_books(self) -> None:
        """--once: fetch every Polymarket US full book once (slow by design; respects rate limit)."""
        ok = 0
        for slug in self.tracked[Venue.POLYMARKET_US]:
            try:
                book = await self.pm.get_orderbook(slug)
                self.md.put(book)
                self.db.insert_snapshots([book])
                ok += 1
            except Exception as e:
                log.warning("polymarket_us full book %s failed: %s", slug, e)
        log.info("pm full books (one pass): %d/%d", ok, len(self.tracked[Venue.POLYMARKET_US]))
