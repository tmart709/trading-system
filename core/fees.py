"""Trading fee models (verified against venue docs on 2026-10-12).

Kalshi (series `fee_type` / `fee_multiplier` come from GET /series/{ticker}):
  quadratic:                 taker = ceil_cent(0.07 * mult * C * P * (1-P)); maker = 0
  quadratic_with_maker_fees: same taker; maker = ceil_cent(0.0175 * mult * C * P * (1-P))
  The 0.07 / 0.0175 coefficients are from Kalshi's fee schedule PDF
  (kalshi.com/docs/kalshi-fee-schedule.pdf). Every KXHIGH* weather series is `quadratic`, mult 1.
  Rounding up to the cent is conservative. Kalshi's exact mechanics (docs: Fee Rounding) depend on
  balance precision and include a per-order rebate accumulator; we never under-estimate.

Polymarket US (docs.polymarket.us/fees):
  taker = theta * C * p * (1-p), theta = market.feeCoefficient (0.0695 standard, 0.10 table tennis),
  rounded to the cent with banker's rounding.
  Maker rebate is -0.0125 * C * p * (1-p) until 2026-10-19 13:00 ET, then volume-tiered
  (Tier 1, < $10M monthly maker volume, gets none). We model maker rebate as 0 by default.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal

CENT = Decimal("0.01")

KALSHI_TAKER_COEF = Decimal("0.07")
KALSHI_MAKER_COEF = Decimal("0.0175")
POLYMARKET_US_DEFAULT_TAKER_THETA = Decimal("0.0695")


def _variance_term(contracts: Decimal, price: Decimal) -> Decimal:
    if not (Decimal(0) <= price <= Decimal(1)):
        raise ValueError(f"price out of range: {price}")
    if contracts < 0:
        raise ValueError(f"negative contracts: {contracts}")
    return contracts * price * (Decimal(1) - price)


def kalshi_fee(
    contracts: Decimal,
    price: Decimal,
    *,
    is_maker: bool = False,
    fee_type: str = "quadratic",
    fee_multiplier: Decimal = Decimal(1),
) -> Decimal:
    """Total fee in dollars for one order of `contracts` at `price`."""
    if fee_type not in ("quadratic", "quadratic_with_maker_fees"):
        # flat / combo schedules aren't modeled; refuse rather than guess.
        raise NotImplementedError(f"Kalshi fee_type {fee_type!r} not modeled")
    if is_maker:
        if fee_type != "quadratic_with_maker_fees":
            return Decimal(0)
        coef = KALSHI_MAKER_COEF
    else:
        coef = KALSHI_TAKER_COEF
    raw = coef * Decimal(fee_multiplier) * _variance_term(contracts, price)
    return raw.quantize(CENT, rounding=ROUND_CEILING)


def polymarket_us_fee(
    contracts: Decimal,
    price: Decimal,
    *,
    is_maker: bool = False,
    theta: Decimal = POLYMARKET_US_DEFAULT_TAKER_THETA,
    maker_rebate_theta: Decimal = Decimal(0),
) -> Decimal:
    """Fee in dollars (negative = rebate). `price` is the traded instrument price.

    Shorting the instrument (taking NO) at p pays the same p*(1-p) fee as buying at p.
    """
    t = Decimal(maker_rebate_theta) if is_maker else Decimal(theta)
    raw = t * _variance_term(contracts, price)
    return raw.quantize(CENT, rounding=ROUND_HALF_EVEN)


def fee_per_contract(total_fee: Decimal, contracts: Decimal) -> Decimal:
    return total_fee / contracts if contracts else Decimal(0)


def taker_fee(venue: str, contracts: Decimal, price: Decimal, params: dict | None = None) -> Decimal:
    """Venue-neutral taker fee for one order. `params`:
    kalshi: {"fee_type", "fee_multiplier"} from the series; polymarket_us: {"theta"} = market.feeCoefficient."""
    params = params or {}
    if venue == "kalshi":
        return kalshi_fee(contracts, price, fee_type=params.get("fee_type", "quadratic"),
                          fee_multiplier=Decimal(str(params.get("fee_multiplier", 1))))
    if venue == "polymarket_us":
        return polymarket_us_fee(contracts, price,
                                 theta=Decimal(str(params.get("theta", POLYMARKET_US_DEFAULT_TAKER_THETA))))
    raise ValueError(f"unknown venue {venue}")
