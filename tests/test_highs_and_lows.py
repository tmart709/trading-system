from core.config import CityConfig, load_config
from core.dashboard.server import _event_parts

DEN = CityConfig("DEN", "KXHIGHDEN", None, "KDEN", "CLIDEN", 39.85, -104.66, "America/Denver",
                 name="Denver", kalshi_low_series="KXLOWTDEN")


def test_series_to_variable():
    assert DEN.variable_for_series("KXHIGHDEN") == "high"
    assert DEN.variable_for_series("KXLOWTDEN") == "low"
    assert DEN.variable_for_series("KXHIGHNY") is None
    assert DEN.kalshi_series_for("low") == "KXLOWTDEN"


def test_event_key_parsing_new_and_legacy():
    assert _event_parts("weather:DEN:low:2026-10-13") == ("DEN", "low", "2026-10-13")
    assert _event_parts("weather:NYC:2026-10-13") == ("NYC", "high", "2026-10-13")   # pre-lows format
    assert _event_parts(None) == (None, None, None)


def test_config_has_all_cities_with_both_series():
    cfg = load_config()
    assert len(cfg.cities) == 24
    for c in cfg.cities.values():
        assert c.kalshi_series and c.kalshi_low_series and c.station.startswith("K")
        assert c.cli_code == "CLI" + c.station[1:]
    assert {c.code for c in cfg.cities.values() if c.polymarket_event_prefix} == {"NYC", "CHI", "MIA", "LAX", "SFO"}


def test_ensemble_fetch_handles_multiple_models(monkeypatch):
    """Regression: a loop variable shadowed `days`, so every model after the first failed."""
    import asyncio
    from datetime import datetime, timedelta
    from core.data import forecasts as fc

    start = datetime(2026, 10, 12)
    hourly = {"time": [(start + timedelta(hours=i)).isoformat() for i in range(96)],
              "temperature_2m": [50 + (i % 24) / 2 for i in range(96)],
              "temperature_2m_member01": [48 + (i % 24) / 2 for i in range(96)]}
    calls = []

    async def fake_get(url, params=None, retries=4):
        calls.append(params["models"])
        return {"hourly": hourly}

    w = fc.WeatherSources("test", ["gfs_seamless", "ecmwf_ifs025"])
    monkeypatch.setattr(w._h, "get", fake_get)
    rows = asyncio.run(w.ensemble_forecasts(DEN))
    asyncio.run(w.aclose())
    assert calls == ["gfs_seamless", "ecmwf_ifs025"]
    assert {r["model"] for r in rows} == {"gfs_seamless", "ecmwf_ifs025"}
    assert {r["variable"] for r in rows} == {"high", "low"}
