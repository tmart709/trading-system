"""Conservative cross-venue matching for daily-high temperature markets.

A candidate pair requires: same city config, same local date, identical integer bracket, and both rules
texts naming the configured station. Everything else (settlement source, timing, rounding) is surfaced
as a warning for the human reviewer. Pairs are always inserted as 'pending'. Only a person approves.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from core.config import CityConfig
from core.models import Market

_KALSHI_DATE = re.compile(r"-(\d{2}[A-Z]{3}\d{2})$")
_PM_DATE = re.compile(r"-(\d{4}-\d{2}-\d{2})$")


def kalshi_event_date(event_ticker: str) -> date | None:
    m = _KALSHI_DATE.search(event_ticker)
    return datetime.strptime(m[1], "%y%b%d").date() if m else None


def pm_event_date(event_slug: str) -> date | None:
    m = _PM_DATE.search(event_slug)
    return date.fromisoformat(m[1]) if m else None


def _norm_source(s: str) -> str:
    s = s.lower()
    if "weather company" in s:
        return "the_weather_company"
    if "climatological report" in s or "national weather service" in s or "nws" in s:
        return "nws_cli"
    return s.strip()


def match_weather_markets(
    city: CityConfig, kalshi: list[Market], pm: list[Market]
) -> list[tuple[Market, Market, str, list[str]]]:
    by_key: dict[tuple, Market] = {}
    for p in pm:
        d = pm_event_date(p.event_id)
        if d and p.bracket:
            by_key[(d, p.bracket)] = p

    out = []
    for k in kalshi:
        d = kalshi_event_date(k.event_id)
        if not (d and k.bracket):
            continue
        p = by_key.get((d, k.bracket))
        if not p:
            continue
        warnings: list[str] = []
        if city.cli_code not in k.rules:
            warnings.append(f"Kalshi rules do not mention {city.cli_code}")
        if city.station not in p.rules:
            warnings.append(f"Polymarket US rules do not mention {city.station}")
        ks, ps = _norm_source(k.settlement_source), _norm_source(p.settlement_source)
        if ks != ps:
            warnings.append(
                f"SETTLEMENT SOURCE DIFFERS: Kalshi={k.settlement_source!r} vs Polymarket US={p.settlement_source!r}. "
                "Values can disagree (rounding, preliminary vs final, revisions)."
            )
        reason = f"{city.code} {d.isoformat()} high temp bracket {k.bracket.label()}F"
        out.append((k, p, reason, warnings))
    return out
