from decimal import Decimal as D

import pytest

from core.fees import kalshi_fee, polymarket_us_fee


# ---------------- Kalshi ----------------

def test_kalshi_taker_rounds_up_to_cent():
    # 0.07 * 100 * 0.55 * 0.45 = 1.7325 -> $1.74
    assert kalshi_fee(D(100), D("0.55")) == D("1.74")


def test_kalshi_single_contract_minimum_cent():
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> $0.02
    assert kalshi_fee(D(1), D("0.50")) == D("0.02")
    # even a tiny fee rounds up to one cent
    assert kalshi_fee(D(1), D("0.01")) == D("0.01")


def test_kalshi_maker_free_on_quadratic_series():
    assert kalshi_fee(D(100), D("0.50"), is_maker=True, fee_type="quadratic") == D("0")


def test_kalshi_maker_fee_series():
    # 0.0175 * 100 * 0.25 = 0.4375 -> $0.44
    assert kalshi_fee(D(100), D("0.50"), is_maker=True, fee_type="quadratic_with_maker_fees") == D("0.44")


def test_kalshi_fee_multiplier():
    assert kalshi_fee(D(100), D("0.50"), fee_multiplier=D("0.5")) == D("0.88")  # 0.875 -> up


def test_kalshi_unmodeled_fee_type_refuses():
    with pytest.raises(NotImplementedError):
        kalshi_fee(D(1), D("0.5"), fee_type="flat")


def test_price_bounds():
    with pytest.raises(ValueError):
        kalshi_fee(D(1), D("1.5"))


# ---------------- Polymarket US (examples from docs.polymarket.us/fees) ----------------

@pytest.mark.parametrize("contracts,price,expected", [
    (1000, "0.10", "6.26"),    # Example 1 (6.255 -> banker's -> 6.26)
    (1000, "0.65", "15.81"),   # Example 2
    (1000, "0.30", "14.60"),   # Example 3
    (1000, "0.90", "6.26"),    # Example 4
    (1000, "0.50", "17.38"),   # Example 5 (17.375 -> 17.38)
    (100, "0.50", "1.74"),     # schedule table
    (100, "0.01", "0.07"),     # schedule table
])
def test_polymarket_us_taker_matches_docs(contracts, price, expected):
    assert polymarket_us_fee(D(contracts), D(price)) == D(expected)


def test_polymarket_us_table_tennis_theta():
    assert polymarket_us_fee(D(1000), D("0.50"), theta=D("0.10")) == D("25.00")  # Example 6


def test_polymarket_us_bankers_rounding_half_even():
    # 0.0695 * 1 * 0.5 * 0.5 = 0.017375 -> 0.02; construct an exact half-cent case:
    # theta=0.02, C=1, p=0.5 -> 0.005 -> half-even -> 0.00
    assert polymarket_us_fee(D(1), D("0.5"), theta=D("0.02")) == D("0.00")
    # theta=0.06 -> 0.015 -> 0.02
    assert polymarket_us_fee(D(1), D("0.5"), theta=D("0.06")) == D("0.02")


def test_polymarket_us_maker_rebate_default_zero_and_optional():
    assert polymarket_us_fee(D(1000), D("0.10"), is_maker=True) == D("0.00")
    # Pre-Oct-19-2026 flat rebate: -0.0125 * 1000 * 0.09 = -1.125 -> -1.12 (half-even)
    assert polymarket_us_fee(D(1000), D("0.10"), is_maker=True, maker_rebate_theta=D("-0.0125")) == D("-1.12")
