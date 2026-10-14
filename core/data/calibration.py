"""Build and evaluate the weather model's historical calibration set.

Data: NWS CLI highs (IEM archive) + Open-Meteo previous-runs deterministic forecasts at lags 1-3 days.
Evaluation is walk-forward: every day is predicted using only samples dated before it.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, timedelta

from core.config import CityConfig, Config
from core.data.forecasts import DETERMINISTIC_MODELS, NBM_HISTORY_START, NBM_MODEL, WeatherSources
from core.db import Database, utcnow
from strategies.weather_model import fit_calibration, high_distribution

log = logging.getLogger(__name__)

HISTORY_START = date(2024, 1, 1)
LAGS = (1, 2, 3)


async def build_calibration_set(cfg: Config, db: Database, weather: WeatherSources,
                                start: date = HISTORY_START) -> dict:
    end = date.today() - timedelta(days=2)   # CLI finalizes the morning after; leave a margin
    stats = {}
    for city in cfg.cities.values():
        n_act = 0
        for year in range(start.year, end.year + 1):
            actuals = await weather.cli_actuals(city, year)
            with db.conn:
                for d, (hi, lo) in actuals.items():
                    if start <= d <= end:
                        upsert_actual(db, city.code, d, hi, lo)
                        n_act += 1
        n_fc = 0
        for model in (*DETERMINISTIC_MODELS, NBM_MODEL):
            chunk_start = max(start, date.fromisoformat(NBM_HISTORY_START)) if model == NBM_MODEL else start
            while chunk_start <= end:
                chunk_end = min(end, chunk_start + timedelta(days=180))
                runs = await weather.previous_runs(city, model, chunk_start, chunk_end, LAGS)
                with db.conn:
                    for variable, by_lag in runs.items():
                        for lag, by_day in by_lag.items():
                            for d, f in by_day.items():
                                if chunk_start <= d <= chunk_end:
                                    db.conn.execute(
                                        "INSERT OR REPLACE INTO calibration_samples "
                                        "(city, variable, target_date, lag_days, model, forecast_f) VALUES (?,?,?,?,?,?)",
                                        (city.code, variable, d.isoformat(), lag, model, round(f, 2)))
                                    n_fc += 1
                chunk_start = chunk_end + timedelta(days=1)
        stats[city.code] = {"actual_days": n_act, "forecast_rows": n_fc}
        log.info("calibration data %s: %s", city.code, stats[city.code])
    return stats


def upsert_actual(db: Database, city: str, d: date, high: int, low: int | None) -> None:
    db.conn.execute(
        "INSERT INTO cli_actuals (city, target_date, high_f, low_f, fetched_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT (city, target_date) DO UPDATE SET high_f=excluded.high_f, low_f=excluded.low_f, "
        "fetched_at=excluded.fetched_at", (city, d.isoformat(), high, low, utcnow()))


def load_samples(db: Database, city: str, lag: int, variable: str = "high",
                 models: tuple[str, ...] = DETERMINISTIC_MODELS) -> list[tuple[date, float, int]]:
    """(date, mean forecast of `models`, actual) for dates where every one of `models` is present."""
    actual_col = "a.high_f" if variable == "high" else "a.low_f"
    marks = ",".join("?" * len(models))
    rows = db.query(
        f"""SELECT s.target_date, AVG(s.forecast_f) f, COUNT(*) n, {actual_col} AS actual
           FROM calibration_samples s JOIN cli_actuals a USING (city, target_date)
           WHERE s.city=? AND s.variable=? AND s.lag_days=? AND s.model IN ({marks}) AND {actual_col} IS NOT NULL
           GROUP BY s.target_date HAVING n = ? ORDER BY s.target_date""",
        (city, variable, lag, *models, len(models)))
    return [(date.fromisoformat(r["target_date"]), r["f"], r["actual"]) for r in rows]


def evaluate(db: Database, city: CityConfig, lag: int, window_days: int = 30,
             eval_from: date = date(2025, 1, 1), bracket_width: int = 2, variable: str = "high") -> dict:
    """Walk-forward check. Reports bias/sigma, log score, and reliability of 2-degree bracket probs."""
    samples = load_samples(db, city.code, lag, variable)
    buckets = defaultdict(lambda: [0, 0.0, 0])   # decile -> [count, sum_p, hits]
    n = 0
    logloss = 0.0
    abs_err = []
    in_80 = 0
    for d, f, actual in samples:
        if d < eval_from:
            continue
        cal = fit_calibration(samples, d, window_days)
        if not cal:
            continue
        mu = f + cal.bias
        pmf = high_distribution(mu, cal.sigma)
        n += 1
        abs_err.append(abs(actual - mu))
        logloss -= math.log(max(pmf.get(actual, 0.0), 1e-6))
        z = (actual - mu) / cal.sigma
        in_80 += abs(z) <= 1.2816
        # Every 2-degree bracket around the forecast: predicted prob vs whether it hit.
        start = math.floor(mu) - 8
        for lo in range(start, start + 16, bracket_width):
            p = sum(pmf.get(k, 0.0) for k in range(lo, lo + bracket_width))
            hit = lo <= actual < lo + bracket_width
            b = min(int(p * 10), 9)
            buckets[b][0] += 1
            buckets[b][1] += p
            buckets[b][2] += hit
    if not n:
        return {"n": 0}
    cal_now = fit_calibration(samples, date.today(), window_days)
    return {
        "n": n,
        "mae": sum(abs_err) / n,
        "log_loss_per_day": logloss / n,
        "coverage_80": in_80 / n,     # should be ~0.80 if sigma is right
        "current_bias": cal_now.bias if cal_now else None,
        "current_sigma": cal_now.sigma if cal_now else None,
        "reliability": {f"{b*10:>2}-{b*10+10}%": (c, s / c, h / c) for b, (c, s, h) in sorted(buckets.items()) if c},
    }
