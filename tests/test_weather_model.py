from datetime import date, timedelta

import pytest

from core.models import Bracket
from strategies.weather_model import bracket_probability, fit_calibration, high_distribution, lag_for


def test_pmf_sums_to_one_and_centers_on_mu():
    pmf = high_distribution(72.3, 2.0)
    assert sum(pmf.values()) == pytest.approx(1.0, abs=1e-12)
    mean = sum(k * p for k, p in pmf.items())
    assert mean == pytest.approx(72.3, abs=0.05)
    assert max(pmf, key=pmf.get) == 72


def test_integer_rounding_bins():
    # mu exactly on an integer with tiny sigma -> nearly all mass on that integer
    pmf = high_distribution(70.0, 0.2)
    assert pmf[70] > 0.98


def test_brackets_partition_probability():
    pmf = high_distribution(71.4, 2.2)
    brackets = [Bracket(None, 70), Bracket(71, 72), Bracket(73, 74), Bracket(75, 76), Bracket(77, 78), Bracket(79, None)]
    assert sum(bracket_probability(pmf, b) for b in brackets) == pytest.approx(1.0, abs=1e-12)
    # symmetric check: P(71-72) for N(71.5, s) should be the single largest bracket
    probs = [bracket_probability(pmf, b) for b in brackets]
    assert probs.index(max(probs)) == 1


def test_observed_floor_truncates_and_renormalizes():
    pmf = high_distribution(70.0, 2.0, floor=72)
    assert min(pmf) == 72
    assert sum(pmf.values()) == pytest.approx(1.0, abs=1e-12)
    assert bracket_probability(pmf, Bracket(None, 71)) == 0.0
    assert bracket_probability(pmf, Bracket(None, 70)) == 0.0


def test_floor_far_above_forecast_concentrates_at_floor():
    pmf = high_distribution(60.0, 1.5, floor=80)
    assert pmf[80] == pytest.approx(1.0, abs=1e-6)


def test_floor_below_forecast_has_little_effect():
    a = high_distribution(75.0, 2.0)
    b = high_distribution(75.0, 2.0, floor=66)
    assert bracket_probability(a, Bracket(75, 76)) == pytest.approx(bracket_probability(b, Bracket(75, 76)), abs=1e-4)


def test_fit_calibration_seasonal_window_and_no_lookahead():
    target = date(2026, 7, 15)
    samples = []
    # Summer samples (within window): actual = forecast + 2 (bias +2), alternating +/-1 noise
    for y in (2024, 2025):
        for i in range(-25, 26):
            d = date(y, 7, 15) + timedelta(days=i)
            samples.append((d, 80.0, 82 + (1 if i % 2 else -1)))
    # Winter samples (outside window) with a huge bias that must be ignored
    for i in range(60):
        samples.append((date(2025, 1, 1) + timedelta(days=i), 30.0, 40))
    # Future sample that must be excluded
    samples.append((date(2026, 7, 20), 80.0, 120))
    cal = fit_calibration(samples, target, window_days=30, min_samples=10)
    assert cal.bias == pytest.approx(2.0, abs=0.05)
    assert 0.9 < cal.sigma < 1.2
    assert cal.n == 102


def test_fit_calibration_needs_enough_samples():
    assert fit_calibration([(date(2025, 7, 1), 80.0, 81)], date(2026, 7, 1), min_samples=40) is None


def test_lag_mapping_is_conservative():
    today = date(2026, 10, 13)
    assert lag_for(today, today) == 1
    assert lag_for(today + timedelta(days=1), today) == 2
    assert lag_for(today - timedelta(days=1), today) is None


def test_combine_centers_averages_available_inputs():
    from strategies.weather_model import combine_centers
    w = {"deterministic": 1, "nws_point": 1, "gfs_ensemble_mean": 1, "ecmwf_ensemble_mean": 1}
    mu, sigma = combine_centers({"deterministic": 70.0, "nws_point": 72.0, "gfs_ensemble_mean": None,
                                 "ecmwf_ensemble_mean": 71.0}, w, 2.0)
    assert mu == pytest.approx(71.0)
    # spread variance = ((−1)^2 + 1^2 + 0)/3 = 2/3 -> sigma = sqrt(4 + 2/3)
    assert sigma == pytest.approx((4 + 2 / 3) ** 0.5)


def test_combine_centers_agreement_keeps_calibrated_sigma():
    from strategies.weather_model import combine_centers
    w = {"a": 1, "b": 1}
    assert combine_centers({"a": 70.0, "b": 70.0}, w, 2.2) == (70.0, pytest.approx(2.2))
    assert combine_centers({"a": 70.0, "b": 74.0}, w, 2.2, inflate_for_disagreement=False) == (72.0, 2.2)


def test_combine_centers_weights_and_zero_weight():
    from strategies.weather_model import combine_centers
    mu, _ = combine_centers({"a": 70.0, "b": 80.0}, {"a": 3, "b": 1}, 2.0)
    assert mu == pytest.approx(72.5)
    mu, sigma = combine_centers({"a": 70.0, "b": 80.0}, {"a": 1, "b": 0}, 2.0)
    assert (mu, sigma) == (70.0, 2.0)
    with pytest.raises(ValueError):
        combine_centers({"a": None}, {"a": 1}, 2.0)


def test_this_weeks_nyc_case_no_longer_shows_big_edge():
    """2026-10-13 NYC: deterministic 69.84 (bias-corrected), NWS 72, ensemble means 73.0/71.0 (raw).
    Deterministic-only said P(<=70)=0.62 vs market 0.36. With all inputs it should be near the market."""
    from strategies.weather_model import combine_centers
    bias = -0.16
    w = {"deterministic": 1, "nws_point": 1, "gfs_ensemble_mean": 1, "ecmwf_ensemble_mean": 1}
    mu, sigma = combine_centers({"deterministic": 69.84, "nws_point": 72.0, "gfs_ensemble_mean": 73.0 + bias,
                                 "ecmwf_ensemble_mean": 71.0 + bias}, w, 2.22)
    p = bracket_probability(high_distribution(mu, sigma), Bracket(None, 70))
    assert 0.30 < p < 0.42


def test_ceiling_for_same_day_lows():
    pmf = high_distribution(50.0, 2.0, ceiling=48)
    assert max(pmf) == 48
    assert sum(pmf.values()) == pytest.approx(1.0, abs=1e-12)
    assert bracket_probability(pmf, Bracket(49, None)) == 0.0
    # ceiling far below the forecast concentrates everything at the ceiling
    assert high_distribution(60.0, 1.5, ceiling=40)[40] == pytest.approx(1.0, abs=1e-6)


def test_ceiling_above_forecast_has_little_effect():
    a = high_distribution(45.0, 2.0)
    b = high_distribution(45.0, 2.0, ceiling=55)
    assert bracket_probability(a, Bracket(44, 45)) == pytest.approx(bracket_probability(b, Bracket(44, 45)), abs=1e-4)


def test_lst_daily_stats_high_and_low():
    from datetime import datetime, timezone
    from core.data.forecasts import lst_daily_stats, _complete_days
    tz = "America/New_York"
    base = datetime(2026, 10, 13, 5, tzinfo=timezone.utc)   # 00:00 EST = start of the LST day
    times = [base + timedelta(hours=i) for i in range(24)]
    vals = [50 + (10 if i == 15 else 0) - (8 if i == 6 else 0) for i in range(24)]
    st = lst_daily_stats(times, vals, tz)
    assert st[date(2026, 10, 13)] == (60, 42, 24)
    days = _complete_days(st)
    assert days["high"][date(2026, 10, 13)] == 60 and days["low"][date(2026, 10, 13)] == 42


def test_mixture_single_component_matches_normal():
    from strategies.weather_model import mixture_distribution
    a = mixture_distribution([(71.3, 2.1, 1.0)])
    b = high_distribution(71.3, 2.1)
    assert a == b


def test_mixture_tails_thinner_than_inflated_normal():
    """Two disagreeing inputs: the mixture keeps component tails; one inflated normal fattens them."""
    import math
    from strategies.weather_model import mixture_distribution
    mix = mixture_distribution([(70.0, 2.0, 1), (74.0, 2.0, 1)])
    inflated = high_distribution(72.0, math.sqrt(4 + 4))     # same variance: 2^2 + spread 2^2
    far = Bracket(80, None)
    assert bracket_probability(mix, far) < bracket_probability(inflated, far)
    assert sum(mix.values()) == pytest.approx(1.0, abs=1e-12)
