"""Weather fair-value strategy: buy daily-high brackets priced below the calibrated model probability.

Venue-agnostic: works on any venue whose markets carry a parsed Bracket and whose books are in MarketData.
Only BUYS (YES or NO) and holds to settlement; no exits. See strategies/weather_model.py for the model.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from core.alerts.notifier import Notifier
from core.config import CityConfig, Config
from core.data.calibration import load_samples
from core.data.forecasts import DETERMINISTIC_MODELS, NBM_MODEL, lst_date, standard_offset
from core.db import Database, utcnow
from core.execution.paper import PaperBroker
from core.execution.portfolio import positions
from core.fees import taker_fee
from core.marketdata import MarketData
from core.matching.weather import kalshi_event_date, pm_event_date
from core.models import Bracket, Side, Venue
from core.risk.manager import ProposedOrder, RiskManager
from core.sizing import size_order
from strategies.weather_model import (Calibration, bracket_probability, combine_centers, fit_calibration,
                                      high_distribution, lag_for)

log = logging.getLogger("strategy.weather")

NAME = "weather_fair_value"
# Bump when the model changes so results can be compared before/after (stored on every signal/prediction).
#   v1 2026-10-12  GFS+ECMWF deterministic blend; NWS + ensemble means; sigma widened on disagreement
#   v2 2026-10-13  calibrated GFS+ECMWF+NBM blend; other inputs nudge the center only; same-day lows off
MODEL_VERSION = "v2"


@dataclass
class CityDay:
    city: CityConfig
    variable: str              # high | low
    target: date
    pmf: dict[int, float]
    mu: float
    sigma: float
    lag: int
    floor: int | None          # high: observed max so far / low: observed min so far (a ceiling)
    forecast_age_min: float
    inputs: dict


class WeatherFairValue:
    name = NAME

    def __init__(self, cfg: Config, db: Database, md: MarketData, risk: RiskManager, broker: PaperBroker,
                 notifier: Notifier, kalshi_fee_params: Callable[[str], dict]):
        self.cfg, self.db, self.md, self.risk, self.broker, self.notifier = cfg, db, md, risk, broker, notifier
        self.p = cfg.strategy(NAME)
        self.kalshi_fee_params = kalshi_fee_params
        self.cities = cfg.strategy_cities(NAME)
        self.variables = list(self.p.get("variables", ["high"]))
        self._samples: dict[tuple[str, str, int], list] = {}
        self._samples_day: date | None = None
        self._last_pred_log: dict[tuple[str, str], datetime] = {}
        self._warned: set[str] = set()

    # ---------------- model ----------------
    def _calibration(self, city: str, variable: str, lag: int, target: date,
                     nbm_weight: float) -> Calibration | None:
        """Bias/sigma fitted on the history of the exact blend used live: (1-w)*mean(GFS,ECMWF) + w*NBM."""
        today = date.today()
        if self._samples_day != today:
            self._samples, self._samples_day = {}, today
        key = (city, variable, lag, nbm_weight)
        if key not in self._samples:
            glob = load_samples(self.db, city, lag, variable)
            if nbm_weight:
                nbm = {d: f for d, f, _ in load_samples(self.db, city, lag, variable, models=(NBM_MODEL,))}
                self._samples[key] = [(d, (1 - nbm_weight) * f + nbm_weight * nbm[d], a)
                                      for d, f, a in glob if d in nbm]
            else:
                self._samples[key] = glob
        return fit_calibration(self._samples[key], target, self.p["calibration_window_days"],
                               min_samples=30 if nbm_weight else 40)

    def _latest_nbm(self, city: str, variable: str, target: date) -> tuple[float, str] | None:
        rows = self.db.query(
            """SELECT point_f, fetched_at FROM forecasts WHERE city=? AND source='open_meteo_forecast'
               AND model=? AND variable=? AND target_date=? ORDER BY fetched_at DESC LIMIT 1""",
            (city, NBM_MODEL, variable, target.isoformat()))
        return (rows[0]["point_f"], rows[0]["fetched_at"]) if rows and rows[0]["point_f"] is not None else None

    def _latest_forecasts(self, city: str, variable: str, target: date) -> tuple[float, float, dict] | None:
        rows = self.db.query(
            """SELECT model, point_f, fetched_at FROM forecasts f
               WHERE city=? AND source='open_meteo_forecast' AND variable=? AND target_date=?
                 AND fetched_at = (SELECT MAX(fetched_at) FROM forecasts g WHERE g.city=f.city
                     AND g.source=f.source AND g.model=f.model AND g.variable=f.variable
                     AND g.target_date=f.target_date)""",
            (city, variable, target.isoformat()))
        got = {r["model"]: (r["point_f"], r["fetched_at"]) for r in rows}
        if not all(m in got for m in DETERMINISTIC_MODELS):
            return None
        blend = sum(got[m][0] for m in DETERMINISTIC_MODELS) / len(DETERMINISTIC_MODELS)
        oldest = min(datetime.fromisoformat(got[m][1]) for m in DETERMINISTIC_MODELS)
        age_min = (datetime.now(timezone.utc) - oldest).total_seconds() / 60
        return blend, age_min, {m: round(got[m][0], 2) for m in DETERMINISTIC_MODELS}

    def _observed_bound(self, city: CityConfig, variable: str, target: date) -> int | None:
        """High: warmest reading so far (a floor). Low: coldest reading so far (a ceiling)."""
        off = standard_offset(city.tz, target.year)
        start = datetime(target.year, target.month, target.day, tzinfo=timezone.utc) - off
        end = start + timedelta(days=1)
        rows = self.db.query(
            "SELECT temp_f, raw_json FROM observations WHERE city=? AND obs_time>=? AND obs_time<? AND temp_f IS NOT NULL",
            (city.code, start.isoformat(), end.isoformat()))
        vals = [r["temp_f"] for r in rows if (json.loads(r["raw_json"] or "{}").get("qc") or "") != "X"]
        # METAR temps are whole-degF readings converted to 0.1 degC; rounding back recovers the reading.
        if not vals:
            return None
        return round(max(vals)) if variable == "high" else round(min(vals))

    def city_day(self, city: CityConfig, variable: str, target: date, now: datetime) -> CityDay | None:
        today_lst = lst_date(now, city.tz)
        lag = lag_for(target, today_lst)
        if lag is None or lag > 3:
            return None
        if target == today_lst and variable not in self.p.get("same_day_variables", ["high", "low"]):
            return None
        if target == today_lst:
            # The model has no intraday "is the peak/trough over yet?" logic, so late in the day it would keep
            # assigning probability beyond the observed extreme. Don't trade same-day markets after the cutoff.
            lst_hour = (now + standard_offset(city.tz, now.year)).hour
            if lst_hour >= self.p["same_day_cutoff_hour_lst"]:
                return None
        fc = self._latest_forecasts(city.code, variable, target)
        if not fc:
            return None
        glob, age_min, inputs = fc
        nbm = self._latest_nbm(city.code, variable, target)
        w = float(self.p.get("nbm_weight", 0)) if nbm else 0.0   # no live NBM -> GFS+ECMWF alone, its own calibration
        if nbm:
            inputs["ncep_nbm"] = round(nbm[0], 2)
            age_min = max(age_min, (datetime.now(timezone.utc) - datetime.fromisoformat(nbm[1])).total_seconds() / 60)
        blend = (1 - w) * glob + w * nbm[0] if w else glob
        cal = self._calibration(city.code, variable, lag, target, w)
        if not cal:
            return None
        bound = (self._observed_bound(city, variable, target)
                 if (self.p["use_observed_floor"] and target == today_lst) else None)
        others = self._other_inputs(city.code, variable, target)
        centers = {
            "calibrated_blend": blend + cal.bias,
            "nws_point": others.get("nws_point"),
            # ensemble means are grid-point model output like the deterministic runs: same bias correction
            "gfs_ensemble_mean": others["gfs_ensemble_mean"] + cal.bias if "gfs_ensemble_mean" in others else None,
            "ecmwf_ensemble_mean": others["ecmwf_ensemble_mean"] + cal.bias if "ecmwf_ensemble_mean" in others else None,
        }
        mu, sigma = combine_centers(centers, self.p["input_weights"], cal.sigma,
                                    self.p.get("inflate_sigma_for_disagreement", True))
        pmf = (high_distribution(mu, sigma, floor=bound) if variable == "high"
               else high_distribution(mu, sigma, ceiling=bound))
        return CityDay(city, variable, target, pmf, mu, sigma, lag, bound, age_min,
                       {**inputs, "blend": round(blend, 2), "bias": round(cal.bias, 2), "n": cal.n,
                        "calibrated_sigma": round(cal.sigma, 2),
                        "centers": {k: round(v, 2) for k, v in centers.items() if v is not None}})

    def _other_inputs(self, city: str, variable: str, target: date) -> dict[str, float]:
        """Latest NWS point forecast and ensemble means for the date, if fresh enough to use."""
        max_age = timedelta(minutes=self.cfg.risk["max_forecast_age_minutes"])
        cutoff = (datetime.now(timezone.utc) - max_age).isoformat()
        wanted = {("nws", "nws_point"): "nws_point",
                  ("open_meteo_ensemble", "gfs_seamless"): "gfs_ensemble_mean",
                  ("open_meteo_ensemble", "ecmwf_ifs025"): "ecmwf_ensemble_mean"}
        rows = self.db.query(
            """SELECT source, model, point_f FROM forecasts f
               WHERE city=? AND variable=? AND target_date=? AND fetched_at >= ? AND point_f IS NOT NULL
                 AND fetched_at = (SELECT MAX(fetched_at) FROM forecasts g WHERE g.city=f.city
                     AND g.source=f.source AND g.model=f.model AND g.variable=f.variable
                     AND g.target_date=f.target_date)""",
            (city, variable, target.isoformat(), cutoff))
        return {wanted[(r["source"], r["model"])]: r["point_f"] for r in rows if (r["source"], r["model"]) in wanted}

    # ---------------- markets ----------------
    def _open_markets(self, city: CityConfig, now: datetime) -> list[dict]:
        fresh = (now - timedelta(minutes=30)).isoformat()
        rows = self.db.query(
            """SELECT venue, market_id, event_id, COALESCE(event_key, event_id) AS event_key, series_id,
                      bracket_low, bracket_high, close_time,
                      json_extract(raw_json, '$.feeCoefficient') AS pm_theta
               FROM markets
               WHERE last_seen >= ? AND result IS NULL AND close_time > ?
                 AND (bracket_low IS NOT NULL OR bracket_high IS NOT NULL)
                 AND ((venue='kalshi' AND series_id IN (?, ?)) OR (venue='polymarket_us' AND event_id LIKE ?))""",
            (fresh, now.isoformat(), city.kalshi_series or "~", city.kalshi_low_series or "~",
             (city.polymarket_event_prefix or "~") + "-%"))
        out = []
        for r in rows:
            if r["venue"] == "kalshi":
                d, variable = kalshi_event_date(r["event_id"]), city.variable_for_series(r["series_id"])
            else:
                d, variable = pm_event_date(r["event_id"]), "high"   # Polymarket US lists highs only
            if d and variable in self.variables:
                out.append({**dict(r), "target": d, "variable": variable,
                            "bracket": Bracket(r["bracket_low"], r["bracket_high"])})
        return out

    def _fee_fn(self, m: dict):
        if m["venue"] == "kalshi":
            params = self.kalshi_fee_params(m["series_id"])
        else:
            params = {"theta": m["pm_theta"]} if m["pm_theta"] is not None else {}
        return lambda c, p: taker_fee(m["venue"], c, p, params)

    # ---------------- loop ----------------
    async def on_tick(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        stats = {"markets": 0, "signals": 0, "orders": 0, "blocked": 0}
        modes = {v.value: self.cfg.effective_mode(NAME, v.value) for v in Venue}
        if all(m == "record" for m in modes.values()):
            return stats
        for v, m in modes.items():
            if m in ("demo", "live") and v not in self._warned:
                self._warned.add(v)
                log.warning("%s: %s mode for %s is not implemented yet; only paper trades will be placed", NAME, m, v)

        halted = self.risk.enforce_daily_loss("paper", self.md, now)
        if halted:
            await self.notifier.send("critical", f"[paper] {halted}")

        current = positions(self.db, "paper")
        for city in self.cities:
            markets = self._open_markets(city, now)
            days = {(v, d): self.city_day(city, v, d, now) for v, d in {(m["variable"], m["target"]) for m in markets}}
            for m in markets:
                cd = days.get((m["variable"], m["target"]))
                if cd is None or modes[m["venue"]] == "record":
                    continue
                stats["markets"] += 1
                await self._evaluate(m, cd, now, stats, current)
                current = positions(self.db, "paper") if stats["orders"] else current
        return stats

    async def _evaluate(self, m: dict, cd: CityDay, now: datetime, stats: dict, current) -> None:
        venue = Venue(m["venue"])
        p_yes = bracket_probability(cd.pmf, m["bracket"])
        got = self.md.get(venue, m["market_id"])
        book, as_of = got if got else (None, None)
        self._log_prediction(m, p_yes, book, cd, now)
        if book is None:
            return
        book_age = (now - as_of).total_seconds()
        fee_fn = self._fee_fn(m)
        close_time = datetime.fromisoformat(m["close_time"]) if m["close_time"] else None

        held = {(p.venue, p.market_id, p.side) for p in current}
        for side, prob in ((Side.YES, p_yes), (Side.NO, 1 - p_yes)):
            if self.p.get("max_entries_per_market_side") and (m["venue"], m["market_id"], side.value) in held:
                continue   # already bet this bracket/side
            prob_d = Decimal(str(round(prob, 6)))
            best = book.best_ask(side)
            if best is None:
                continue
            best_edge = prob_d - best.price - fee_fn(Decimal(100), best.price) / 100
            if best_edge < Decimal(str(self.p["min_edge"])):
                continue
            details = {"model_version": MODEL_VERSION, "variable": cd.variable, "mu": round(cd.mu, 2), "sigma": round(cd.sigma, 2), "lag": cd.lag,
                       "observed_bound": cd.floor,
                       "inputs": cd.inputs, "bracket": m["bracket"].label(), "target": cd.target.isoformat(),
                       "book_age_s": round(book_age, 1)}
            if best_edge > Decimal(str(self.p["max_edge"])):
                # An edge this big against a live market is more likely a model error than free money.
                stats["suspect"] = stats.get("suspect", 0) + 1
                self._log_signal(m, side, prob_d, best.price, fee_fn, best_edge, None,
                                 {**details, "plan": f"suspect: edge > {self.p['max_edge']}, not traded"})
                log.info("suspect edge %.3f on %s %s %s (model %.3f vs ask %s); not trading",
                         best_edge, m["venue"], m["market_id"], side.value, prob, best.price)
                continue
            room = self.risk.headroom(m["venue"], m["market_id"], m["event_key"], "paper", now, current)
            plan, why = size_order(
                prob_d, book.asks(side), fee_fn,
                min_edge=Decimal(str(self.p["min_edge"])),
                min_book_contracts=Decimal(str(self.p["min_book_contracts"])),
                bankroll=Decimal(str(self.cfg.risk["paper_bankroll_usd"])),
                kelly_fraction=Decimal(str(self.p["kelly_fraction"])),
                max_contracts=Decimal(str(self.cfg.risk["max_contracts_per_order"])),
                max_cost=room,
            )
            stats["signals"] += 1
            details["plan"] = why
            signal_id = self._log_signal(m, side, prob_d, best.price, fee_fn, best_edge, plan, details)
            if plan is None:
                continue
            order = ProposedOrder(NAME, m["venue"], m["market_id"], m["event_key"], side.value, plan.contracts,
                                  plan.limit_price, plan.cost, close_time)
            reasons = self.risk.check_order(order, mode="paper", now=now, book_age_s=book_age,
                                            forecast_age_min=cd.forecast_age_min,
                                            min_minutes_to_close=self.p["min_minutes_to_close"], current=current)
            if reasons:
                stats["blocked"] += 1
                self.db.conn.execute("UPDATE signals SET details_json=json_set(details_json, '$.blocked', json(?)) WHERE id=?",
                                     (json.dumps(reasons), signal_id))
                log.info("blocked %s %s %s: %s", m["venue"], m["market_id"], side.value, "; ".join(reasons))
                continue
            res = self.broker.execute(
                client_order_id=f"wfv-{m['venue']}-{m['market_id']}-{side.value}-{now:%Y%m%dT%H%M}",
                strategy=NAME, venue=m["venue"], market_id=m["market_id"], side=side.value,
                contracts=plan.contracts, limit_price=plan.limit_price, book=book, fee_fn=fee_fn, signal_id=signal_id)
            if not res.get("duplicate"):
                stats["orders"] += 1
                self.db.conn.execute("UPDATE signals SET acted=1 WHERE id=?", (signal_id,))
                msg = (f"[paper] BUY {res['filled'].normalize():f} {side.value.upper()} {m['market_id']} ({m['venue']}) "
                       f"@ {res['vwap']:.4f} + fee {res['fee']:.2f} | model {prob:.3f} vs ask {best.price} "
                       f"| edge {plan.edge_per_contract:.3f}/contract")
                await self.notifier.send("info", msg)

    def _log_signal(self, m, side, prob, ask, fee_fn, edge, plan, details) -> int:
        cur = self.db.conn.execute(
            """INSERT INTO signals (ts, strategy, venue, market_id, side, model_prob, market_price,
                   fee_per_contract, edge, size, mode, acted, details_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)""",
            (utcnow(), NAME, m["venue"], m["market_id"], side.value, str(prob), str(ask),
             str((fee_fn(Decimal(100), ask) / 100).quantize(Decimal("0.0001"))), str(edge.quantize(Decimal("0.0001"))),
             str(plan.contracts) if plan else "0", "paper", json.dumps(details)))
        return cur.lastrowid

    def _log_prediction(self, m, p_yes, book, cd: CityDay, now: datetime) -> None:
        key = (m["venue"], m["market_id"])
        last = self._last_pred_log.get(key)
        if last and (now - last).total_seconds() < self.p["prediction_log_minutes"] * 60:
            return
        self._last_pred_log[key] = now
        mid = None
        if book and book.yes_bids and book.yes_asks:
            mid = float((book.yes_bids[0].price + book.yes_asks[0].price) / 2)
        self.db.conn.execute(
            "INSERT INTO model_predictions (ts, strategy, venue, market_id, model_prob, market_mid, details_json) VALUES (?,?,?,?,?,?,?)",
            (utcnow(), NAME, m["venue"], m["market_id"], p_yes, mid,
             json.dumps({"model_version": MODEL_VERSION, "variable": cd.variable, "mu": round(cd.mu, 2), "sigma": round(cd.sigma, 2), "lag": cd.lag,
                         "floor": cd.floor,
                         "centers": cd.inputs.get("centers")})))
