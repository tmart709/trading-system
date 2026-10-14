from datetime import date, datetime, timezone

from core.config import CityConfig
from core.data.forecasts import lst_daily_max, lst_date
from core.exchanges.kalshi import KalshiClient, parse_bracket as k_bracket
from core.exchanges.polymarket_us import PolymarketUSClient, parse_bracket as pm_bracket
from core.matching.weather import kalshi_event_date, match_weather_markets, pm_event_date
from core.models import Bracket

NYC = CityConfig("NYC", "KXHIGHNY", "temp-nychigh", "KNYC", "CLINYC", 40.779, -73.9692, "America/New_York")


def test_kalshi_brackets_from_strikes():
    assert k_bracket({"strike_type": "between", "floor_strike": 71, "cap_strike": 72}) == Bracket(71, 72)
    assert k_bracket({"strike_type": "less", "cap_strike": 71}) == Bracket(None, 70)      # "70 or below"
    assert k_bracket({"strike_type": "greater", "floor_strike": 78}) == Bracket(79, None)  # "79 or above"
    assert k_bracket({"strike_type": "between", "floor_strike": 71.5, "cap_strike": 72}) is None


def test_polymarket_us_brackets_from_description_not_slug():
    # Real wording, 2026-10-13 NYC. Slug says gte71lt72 but the contract is 71-72 inclusive.
    assert pm_bracket("be between 71F and 72F? Outcome verified") == Bracket(71, 72)
    assert pm_bracket("be less than or equal to 70F? Outcome") == Bracket(None, 70)
    assert pm_bracket("be greater than or equal to 79F? Outcome") == Bracket(79, None)
    assert pm_bracket("be strictly less than 40 degrees Fahrenheit") is None  # not a F-suffixed bracket
    assert pm_bracket("be strictly less than 40F on any day") == Bracket(None, 39)


def test_bracket_contains():
    b = Bracket(71, 72)
    assert b.contains(71) and b.contains(72) and not b.contains(73)
    assert Bracket(None, 70).contains(-5) and Bracket(79, None).contains(120)


def test_event_dates():
    assert kalshi_event_date("KXHIGHNY-26OCT13") == date(2026, 10, 13)
    assert pm_event_date("temp-nychigh-2026-10-13") == date(2026, 10, 13)


def _k(ticker, st, lo, hi):
    return KalshiClient.to_market(
        {"ticker": ticker, "event_ticker": "KXHIGHNY-26OCT13", "strike_type": st, "floor_strike": lo,
         "cap_strike": hi, "rules_primary": "If the maximum temperature recorded at New York City (CLINYC) ... "
         "according to The Weather Company", "status": "active"},
        {"ticker": "KXHIGHNY", "settlement_sources": [{"name": "The Weather Company"}]})


def _p(slug, desc):
    return PolymarketUSClient.to_market(
        {"slug": slug, "description": f"Will the highest temperature recorded at Central Park (KNYC) in New York "
         f"City for 2026-10-13 as reported by the National Weather Service's Climatological Report (Daily) be "
         f"{desc}? Outcome verified from NWS Climatological Report."},
        {"slug": "temp-nychigh-2026-10-13"})


def test_matching_pairs_identical_brackets_and_flags_source_mismatch():
    kalshi = [_k("KXHIGHNY-26OCT13-B71.5", "between", 71, 72), _k("KXHIGHNY-26OCT13-T71", "less", None, 71),
              _k("KXHIGHNY-26OCT13-T78", "greater", 78, None)]
    pm = [_p("tc-...-gte71lt72f", "between 71F and 72F"), _p("tc-...-lt71f", "less than or equal to 70F"),
          _p("tc-...-gte80f", "greater than or equal to 80F")]  # deliberately misaligned top bracket
    pairs = match_weather_markets(NYC, kalshi, pm)
    got = {(k.market_id, p.market_id) for k, p, _, _ in pairs}
    assert got == {("KXHIGHNY-26OCT13-B71.5", "tc-...-gte71lt72f"), ("KXHIGHNY-26OCT13-T71", "tc-...-lt71f")}
    for _, _, _, warnings in pairs:
        assert any("SETTLEMENT SOURCE DIFFERS" in w for w in warnings)


def test_pm_settlement_source_extracted():
    m = _p("x", "between 71F and 72F")
    assert m.settlement_source == "NWS Climatological Report"
    m2 = PolymarketUSClient.to_market({"slug": "y", "description": "Will X as reported by the National Weather "
                                       "Service's Climatological Report (Daily) be between 71F and 72F?"})
    assert m2.settlement_source == "National Weather Service's Climatological Report (Daily)"


def test_lst_day_during_dst():
    # In October, NY is on EDT (UTC-4) but the CLI day is EST (UTC-5): 00:00 EST = 05:00 UTC.
    assert lst_date(datetime(2026, 10, 13, 4, 30, tzinfo=timezone.utc), "America/New_York") == date(2026, 10, 12)
    assert lst_date(datetime(2026, 10, 13, 5, 0, tzinfo=timezone.utc), "America/New_York") == date(2026, 10, 13)


def test_lst_daily_max_excludes_late_evening_spike_from_next_day():
    tz = "America/New_York"
    # 00:30 EDT on Oct 13 local clock = 04:30 UTC: still Oct 12 LST, so it counts toward the 12th.
    times = [datetime(2026, 10, 13, 4, 30, tzinfo=timezone.utc), datetime(2026, 10, 13, 18, tzinfo=timezone.utc)]
    out = lst_daily_max(times, [80.0, 70.0], tz)
    assert out[date(2026, 10, 12)] == (80.0, 1)
    assert out[date(2026, 10, 13)] == (70.0, 1)
