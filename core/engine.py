"""Wires the recorder, market data, strategies, risk and execution into one process."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.alerts.notifier import Notifier
from core.config import Config, env
from core.data.calibration import upsert_actual
from core.data.forecasts import WeatherSources
from core.data.recorder import Recorder
from core.db import Database, utcnow
from core.exchanges.kalshi import KalshiClient
from core.exchanges.polymarket_us import PolymarketUSClient
from core.exchanges.polymarket_us_ws import PolymarketMarketStream
from core.execution.paper import PaperBroker
from core.marketdata import MarketData
from core.reporting import daily_report
from core.risk.manager import RiskManager
from strategies.weather_fair_value import WeatherFairValue

log = logging.getLogger("engine")


class Engine:
    def __init__(self, cfg: Config, with_strategies: bool):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.kalshi = KalshiClient("prod", max_rps=cfg.venue("kalshi")["max_requests_per_second"])
        key_id, secret = env("POLYMARKET_KEY_ID"), env("POLYMARKET_SECRET_KEY")
        self.pm = PolymarketUSClient(max_rps=cfg.venue("polymarket_us")["max_requests_per_second"],
                                     book_rps=cfg.venue("polymarket_us")["book_requests_per_second"])
        self.pm_stream = PolymarketMarketStream(key_id, secret) if (key_id and secret) else None
        if not self.pm_stream:
            log.warning("no Polymarket US key: using slow REST polling for its books")
        self.weather = WeatherSources(env(cfg.weather["nws_user_agent_env"], ""), cfg.weather["ensemble_models"])
        self.notifier = Notifier(cfg.raw.get("alerts", {}).get("backend", "stdout"))
        self.md = MarketData(self.pm_stream)
        self.recorder = Recorder(cfg, self.db, self.kalshi, self.pm, self.weather, self.notifier,
                                 md=self.md, pm_stream=self.pm_stream)
        self.tz = cfg.raw.get("reports", {}).get("timezone", "America/New_York")
        self.risk = RiskManager(cfg.risk, self.db, self.tz)
        self.recorder.extra_jobs["refresh_cli_actuals"] = (6 * 3600, self.refresh_cli_actuals)
        if with_strategies:
            self.broker = PaperBroker(self.db)
            self.strategy = WeatherFairValue(cfg, self.db, self.md, self.risk, self.broker, self.notifier,
                                             self.recorder.kalshi_fee_params)
            self.recorder.after_snapshot.append(("weather_fair_value", self.strategy.on_tick))
            self.recorder.extra_jobs["daily_report"] = (300, self.maybe_daily_report)

    async def refresh_cli_actuals(self) -> dict:
        """Keep recent official highs/lows current (calibration + settlement-source agreement tracking)."""
        n = 0
        cutoff = date.today() - timedelta(days=2)   # final CLI is out the morning after
        for city in self.cfg.cities.values():
            with self.db.conn:
                for d, (hi, lo) in (await self.weather.cli_actuals(city, date.today().year)).items():
                    if d <= cutoff:
                        upsert_actual(self.db, city.code, d, hi, lo)
                        n += 1
        return {"cli_rows": n}

    async def maybe_daily_report(self) -> dict:
        """Once a day after the configured local hour: report on the previous day."""
        now = datetime.now(ZoneInfo(self.tz))
        if now.hour < self.cfg.raw.get("reports", {}).get("daily_report_hour_local", 9):
            return {"skipped": "before report hour"}
        day = now.date() - timedelta(days=1)
        key = f"report_{day.isoformat()}"
        if self.db.query("SELECT 1 FROM controls WHERE key=?", (key,)):
            return {"skipped": "already sent"}
        text = self.report(day)
        self.db.conn.execute("INSERT INTO controls (key, value, updated_at) VALUES (?,?,?)", (key, "sent", utcnow()))
        await self.notifier.send("info", text)
        return {"reported": day.isoformat()}

    def report(self, day: date) -> str:
        series_to_city = {}
        for c in self.cfg.cities.values():
            for v in ("high", "low"):
                if c.kalshi_series_for(v):
                    series_to_city[c.kalshi_series_for(v)] = (c.code, v)
        return daily_report(self.db, self.md, day, series_to_city, tz=self.tz)

    async def aclose(self) -> None:
        for c in (self.kalshi, self.pm, self.weather, self.notifier):
            await c.aclose()
        self.db.close()
