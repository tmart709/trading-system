"""Probability model for a city's daily high or low (NWS CLI definition: integer degF, LST day).

    H ~ Normal(mu, sigma), discretized to integers:  P(H = k) = F(k + 0.5) - F(k - 0.5)
    mu    = mean(GFS, ECMWF deterministic LST-day max) + bias
    bias, sigma fitted from historical (forecast, CLI actual) pairs for the same city and lead time,
    using only days within +/- `window_days` of the target's day-of-year (seasonal), across years.
    Same-day: the high observed so far is a hard floor -> truncate below it and renormalize.

Lead time: a market for date D evaluated on local date T uses lag = (D - T) + 1 day runs' error stats,
which is conservative (the live run is fresher than the historical lag it's scored with).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date

from core.models import Bracket


@dataclass(frozen=True)
class Calibration:
    bias: float
    sigma: float
    n: int


def _doy_distance(a: date, b: date) -> int:
    d = abs(a.timetuple().tm_yday - b.timetuple().tm_yday)
    return min(d, 365 - d)


def fit_calibration(
    samples: list[tuple[date, float, int]],   # (target_date, blended_forecast_f, actual_f)
    target: date,
    window_days: int = 30,
    min_samples: int = 40,
    sigma_floor: float = 1.0,
) -> Calibration | None:
    """Seasonal bias/sigma from residuals actual - forecast. Excludes samples on/after `target`."""
    resid = [a - f for d, f, a in samples if d < target and _doy_distance(d, target) <= window_days]
    if len(resid) < min_samples:
        return None
    bias = statistics.fmean(resid)
    sigma = max(statistics.stdev(resid), sigma_floor)
    return Calibration(bias=bias, sigma=sigma, n=len(resid))


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def mixture_distribution(components: list[tuple[float, float, float]], floor: int | None = None,
                         ceiling: int | None = None, span: float = 6.0) -> dict[int, float]:
    """Integer-degree pmf from a weighted mixture of normals [(mu, sigma, weight), ...].

    Each forecast input contributes its own bell curve with its own calibrated width. When inputs
    disagree the mixture is wider in the middle, but its far tails stay those of the components,
    unlike one normal with an inflated sigma, which fattens the tails and overprices long shots.

    floor:   same-day high already observed -> the high can't end lower (H >= floor).
    ceiling: same-day low already observed  -> the low can't end higher (L <= ceiling).
    """
    comps = [(m, s, w) for m, s, w in components if w > 0]
    if not comps:
        raise ValueError("no components")
    wsum = sum(w for _, _, w in comps)
    lo = math.floor(min(m - span * s for m, s, _ in comps))
    hi = math.ceil(max(m + span * s for m, s, _ in comps))
    if floor is not None:
        lo, hi = max(lo, floor), max(hi, floor)
    if ceiling is not None:
        lo, hi = min(lo, ceiling), min(hi, ceiling)
    cdf = lambda x: sum(w * _norm_cdf(x, m, s) for m, s, w in comps) / wsum  # noqa: E731
    pmf = {k: cdf(k + 0.5) - cdf(k - 0.5) for k in range(lo, hi + 1)}
    if floor is None:
        pmf[lo] += cdf(lo - 0.5)
    if ceiling is None:
        pmf[hi] += 1 - cdf(hi + 0.5)
    total = sum(pmf.values())
    if total <= 1e-12:   # observation far beyond the forecast: it dominates
        return {floor if floor is not None else ceiling: 1.0}
    return {k: v / total for k, v in pmf.items()}


def high_distribution(mu: float, sigma: float, floor: int | None = None, ceiling: int | None = None,
                      span: float = 6.0) -> dict[int, float]:
    """Single-normal special case of mixture_distribution."""
    return mixture_distribution([(mu, sigma, 1.0)], floor, ceiling, span)


def bracket_probability(pmf: dict[int, float], bracket: Bracket) -> float:
    return min(1.0, max(0.0, sum(p for k, p in pmf.items() if bracket.contains(k))))


def lag_for(target: date, today_local: date) -> int | None:
    days_ahead = (target - today_local).days
    if days_ahead < 0:
        return None
    return days_ahead + 1


def combine_centers(centers: dict[str, float], weights: dict[str, float], calibrated_sigma: float,
                    inflate_for_disagreement: bool = True) -> tuple[float, float]:
    """Blend several estimates of the day's high into one (mu, sigma).

    `centers` maps input name -> bias-adjusted estimate (degF); inputs missing or weighted 0 are skipped.
    sigma starts from the calibrated error of the deterministic blend (conservative: a blend of more
    inputs is usually more accurate). When inputs disagree, sigma is widened by their weighted spread:
    sigma = sqrt(calibrated^2 + spread^2), so the model gets less confident instead of picking a side.
    """
    used = {k: v for k, v in centers.items() if v is not None and weights.get(k, 0) > 0}
    if not used:
        raise ValueError("no forecast inputs")
    wsum = sum(weights[k] for k in used)
    mu = sum(weights[k] * v for k, v in used.items()) / wsum
    if not inflate_for_disagreement or len(used) < 2:
        return mu, calibrated_sigma
    spread_var = sum(weights[k] * (v - mu) ** 2 for k, v in used.items()) / wsum
    return mu, math.sqrt(calibrated_sigma ** 2 + spread_var)
