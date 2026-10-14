"""Free weather inputs: NWS (official point + gridpoint forecast, station obs) and Open-Meteo ensembles.

Daily highs on the NWS Daily Climate Report (CLI) are computed over the LOCAL STANDARD TIME day
(midnight-midnight LST). During daylight saving time that is 01:00-01:00 local clock time.
Ensemble daily maxima are therefore computed here from hourly members over the LST day, not taken
from Open-Meteo's calendar-day aggregates.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from core.config import CityConfig
from core.ratelimit import TokenBucket, backoff_delay

log = logging.getLogger(__name__)

NWS = "https://api.weather.gov"
OPEN_METEO_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
# Deterministic models used by the fair-value model. They're the inputs the calibration is fitted on
# (same models via the previous-runs archive), so live and historical inputs match exactly.
DETERMINISTIC_MODELS = ("gfs_seamless", "ecmwf_ifs025")


def c_to_f(c: float | None) -> float | None:
    return None if c is None else c * 9 / 5 + 32


def standard_offset(tz: str, year: int) -> timedelta:
    """UTC offset of local standard time (January offset for northern-hemisphere US cities)."""
    return datetime(year, 1, 15, 12, tzinfo=ZoneInfo(tz)).utcoffset()


def lst_date(ts_utc: datetime, tz: str) -> date:
    return (ts_utc + standard_offset(tz, ts_utc.year)).date()


def lst_daily_stats(times_utc: list[datetime], values: list[float | None], tz: str) -> dict[date, tuple[float, float, int]]:
    """(max, min, n_hours) of hourly values per LST day; callers should require ~24 hours."""
    out: dict[date, tuple[float, float, int]] = {}
    for t, v in zip(times_utc, values):
        if v is None:
            continue
        d = lst_date(t, tz)
        cur = out.get(d)
        out[d] = (v, v, 1) if cur is None else (max(cur[0], v), min(cur[1], v), cur[2] + 1)
    return out


def lst_daily_max(times_utc: list[datetime], values: list[float | None], tz: str) -> dict[date, tuple[float, int]]:
    """Max of hourly values per LST day: {day: (max, n_hours)}."""
    return {d: (mx, n) for d, (mx, _, n) in lst_daily_stats(times_utc, values, tz).items()}


def _complete_days(stats: dict[date, tuple[float, float, int]]) -> dict[str, dict[date, float]]:
    """{'high': {day: max}, 'low': {day: min}} for days with (nearly) all 24 hours present."""
    return {"high": {d: mx for d, (mx, _, n) in stats.items() if n >= 23},
            "low": {d: mn for d, (_, mn, n) in stats.items() if n >= 23}}


def _iso_hours(duration: str) -> float:
    """ISO-8601 duration like PT13H / P1DT6H -> hours."""
    import re
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", duration)
    if not m:
        return 0.0
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return d * 24 + h + mi / 60


class _Http:
    def __init__(self, user_agent: str, rps: float = 4.0):
        self.http = httpx.AsyncClient(timeout=30, headers={"User-Agent": user_agent, "Accept": "application/geo+json"})
        self.bucket = TokenBucket(rps)

    async def get(self, url: str, params: dict | None = None, retries: int = 4) -> Any:
        for attempt in range(retries + 1):
            await self.bucket.acquire()
            try:
                r = await self.http.get(url, params=params)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    await asyncio.sleep(backoff_delay(attempt, base=2.0))
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt == retries:
                    raise
                await asyncio.sleep(backoff_delay(attempt, base=2.0))

    async def aclose(self) -> None:
        await self.http.aclose()


class WeatherSources:
    def __init__(self, user_agent: str, ensemble_models: list[str]):
        if not user_agent or "example.com" in user_agent:
            log.warning("NWS_USER_AGENT not set to real contact info; api.weather.gov may block requests")
        self._h = _Http(user_agent or "trading-system")
        self.models = ensemble_models
        self._points: dict[str, dict] = {}

    async def aclose(self) -> None:
        await self._h.aclose()

    async def _point(self, city: CityConfig) -> dict:
        if city.code not in self._points:
            self._points[city.code] = (await self._h.get(f"{NWS}/points/{city.lat},{city.lon}"))["properties"]
        return self._points[city.code]

    async def nws_forecasts(self, city: CityConfig) -> list[dict]:
        """Official NWS point forecast (daytime highs, overnight lows) + gridpoint max/minTemperature.

        Each value is assigned to the local date of its period's midpoint: a 6pm-6am overnight low lands on
        the following morning's date, which is when the CLI daily low usually occurs (approximation: the CLI
        low is the LST calendar-day minimum and occasionally falls late in the evening instead)."""
        pt = await self._point(city)
        tz = ZoneInfo(city.tz)
        rows: list[dict] = []
        fc = (await self._h.get(pt["forecast"]))["properties"]
        for p in fc.get("periods", []):
            temp = p["temperature"]
            if isinstance(temp, dict):  # newer schema uses quantitative values
                temp = c_to_f(temp.get("value")) if "degC" in temp.get("unitCode", "") else temp.get("value")
            start, end = datetime.fromisoformat(p["startTime"]), datetime.fromisoformat(p["endTime"])
            mid = (start + (end - start) / 2).astimezone(tz)
            rows.append({
                "city": city.code, "source": "nws", "model": "nws_point",
                "variable": "high" if p.get("isDaytime") else "low",
                "target_date": mid.date().isoformat(), "issued_at": fc.get("updateTime") or fc.get("generatedAt"),
                "point_f": float(temp) if temp is not None else None, "members": None,
                "raw": {"name": p.get("name"), "shortForecast": p.get("shortForecast")},
            })
        grid = (await self._h.get(pt["forecastGridData"]))["properties"]
        for field, variable in (("maxTemperature", "high"), ("minTemperature", "low")):
            mt = grid.get(field, {})
            unit = mt.get("uom", "")
            for v in mt.get("values", []):
                start_s, dur = v["validTime"].split("/")
                start = datetime.fromisoformat(start_s)
                hours = _iso_hours(dur)
                mid = (start + timedelta(hours=hours / 2)).astimezone(tz)
                val = v["value"]
                rows.append({
                    "city": city.code, "source": "nws", "model": "nws_grid", "variable": variable,
                    "target_date": mid.date().isoformat(),
                    "issued_at": grid.get("updateTime"), "point_f": c_to_f(val) if "degC" in unit else val,
                    "members": None, "raw": {"validTime": v["validTime"], "value": val, "uom": unit},
                })
        return rows

    async def ensemble_forecasts(self, city: CityConfig, days: int = 3) -> list[dict]:
        rows: list[dict] = []
        for model in self.models:
            try:
                d = await self._h.get(OPEN_METEO_ENSEMBLE, {
                    "latitude": city.lat, "longitude": city.lon, "hourly": "temperature_2m",
                    "temperature_unit": "fahrenheit", "timezone": "GMT", "models": model,
                    "forecast_days": days + 1, "past_days": 1,
                })
            except Exception as e:  # one bad model shouldn't drop the others
                log.warning("open-meteo %s failed for %s: %s", model, city.code, e)
                continue
            h = d.get("hourly") or {}
            times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in h.get("time", [])]
            member_keys = sorted(k for k in h if k.startswith("temperature_2m"))
            per_member = [_complete_days(lst_daily_stats(times, h[k], city.tz)) for k in member_keys]
            for variable in ("high", "low"):
                target_days = sorted({day for m in per_member for day in m[variable]})
                for day in target_days:
                    vals = [m[variable][day] for m in per_member if day in m[variable]]
                    rows.append({
                        "city": city.code, "source": "open_meteo_ensemble", "model": model, "variable": variable,
                        "target_date": day.isoformat(), "issued_at": None,
                        "point_f": sum(vals) / len(vals), "members": [round(v, 2) for v in vals], "raw": None,
                    })
        return rows

    async def deterministic_forecasts(self, city: CityConfig, days: int = 3) -> list[dict]:
        rows: list[dict] = []
        issued = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for model in DETERMINISTIC_MODELS:
            try:
                d = await self._h.get(OPEN_METEO_FORECAST, {
                    "latitude": city.lat, "longitude": city.lon, "hourly": "temperature_2m",
                    "temperature_unit": "fahrenheit", "timezone": "GMT", "models": model,
                    "forecast_days": days + 1, "past_days": 1,
                })
            except Exception as e:
                log.warning("open-meteo forecast %s failed for %s: %s", model, city.code, e)
                continue
            h = d.get("hourly") or {}
            times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in h.get("time", [])]
            daily = _complete_days(lst_daily_stats(times, h.get("temperature_2m", []), city.tz))
            for variable, by_day in daily.items():
                for day, val in sorted(by_day.items()):
                    rows.append({"city": city.code, "source": "open_meteo_forecast", "model": model,
                                 "variable": variable, "target_date": day.isoformat(), "issued_at": issued,
                                 "point_f": val, "members": None, "raw": None})
        return rows

    async def previous_runs(self, city: CityConfig, model: str, start: date, end: date,
                            lags: tuple[int, ...] = (1, 2, 3)) -> dict[str, dict[int, dict[date, float]]]:
        """Historical forecasts {variable: {lag_days: {LST date: degF}}} from Open-Meteo's previous-runs archive."""
        d = await self._h.get(OPEN_METEO_PREVIOUS_RUNS, {
            "latitude": city.lat, "longitude": city.lon, "models": model,
            "hourly": ",".join(f"temperature_2m_previous_day{l}" for l in lags),
            "temperature_unit": "fahrenheit", "timezone": "GMT",
            "start_date": start.isoformat(), "end_date": end.isoformat(),
        })
        h = d.get("hourly") or {}
        times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in h.get("time", [])]
        out: dict[str, dict[int, dict[date, float]]] = {"high": {}, "low": {}}
        for lag in lags:
            daily = _complete_days(lst_daily_stats(times, h.get(f"temperature_2m_previous_day{lag}", []), city.tz))
            for variable in ("high", "low"):
                out[variable][lag] = daily[variable]
        return out

    async def cli_actuals(self, city: CityConfig, year: int) -> dict[date, tuple[int, int | None]]:
        """Official NWS Daily Climate Report (high, low) via the Iowa Environmental Mesonet archive."""
        d = await self._h.get("https://mesonet.agron.iastate.edu/json/cli.py",
                              {"station": city.station, "year": year})
        out = {}
        for r in d.get("results", []):
            hi, lo = r.get("high"), r.get("low")
            if isinstance(hi, (int, float)):
                out[date.fromisoformat(r["valid"])] = (int(hi), int(lo) if isinstance(lo, (int, float)) else None)
        return out

    async def observations(self, city: CityConfig, limit: int = 30) -> list[dict]:
        d = await self._h.get(f"{NWS}/stations/{city.station}/observations", {"limit": limit})
        out = []
        for f in d.get("features", []):
            p = f["properties"]
            t = p.get("temperature") or {}
            out.append({
                "city": city.code, "station": city.station, "obs_time": p["timestamp"],
                "temp_f": c_to_f(t.get("value")) if "degC" in (t.get("unitCode") or "") else t.get("value"),
                "raw": {"qc": t.get("qualityControl"), "max24h": p.get("maxTemperatureLast24Hours"),
                        "textDescription": p.get("textDescription")},
            })
        return out
