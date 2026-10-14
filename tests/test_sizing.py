from decimal import Decimal as D

import pytest

from core.fees import kalshi_fee, taker_fee
from core.models import Level
from core.sizing import kelly_fraction_binary, plan_for_size, qualifying_depth, size_order

KFEE = lambda c, p: kalshi_fee(c, p)  # noqa: E731
NOFEE = lambda c, p: D(0)  # noqa: E731
ASKS = [Level(D("0.30"), D(10)), Level(D("0.32"), D(20)), Level(D("0.40"), D(100))]


def kw(**over):
    base = dict(min_edge=D("0.05"), min_book_contracts=D(5), bankroll=D(1000), kelly_fraction=D("0.1"),
                max_contracts=D(200), max_cost=D(1000))
    base.update(over)
    return base


def test_kelly_formula():
    # p=0.5 paying 0.4: f* = (0.5-0.4)/(1-0.4) = 1/6
    assert kelly_fraction_binary(D("0.5"), D("0.4")) == pytest.approx(D(1) / D(6))
    assert kelly_fraction_binary(D("0.3"), D("0.4")) == 0      # negative edge -> no bet
    assert kelly_fraction_binary(D("0.9"), D("1.0")) == 0


def test_qualifying_depth_stops_at_first_bad_level():
    # p=0.40, min edge 0.05: 0.30 (+~0.015 fee) ok, 0.32 (+~0.015) ok, 0.40 not
    lv = qualifying_depth(D("0.40"), ASKS, KFEE, D("0.05"))
    assert [l.price for l in lv] == [D("0.30"), D("0.32")]


def test_plan_vwap_and_fee():
    plan = plan_for_size(D("0.5"), ASKS, D(20), KFEE)
    assert plan.contracts == 20
    assert plan.vwap == D("0.31")                     # 10 @ .30 + 10 @ .32
    assert plan.fee == kalshi_fee(D(20), D("0.31"))   # 0.07*20*.31*.69 = 0.29946 -> 0.30
    assert plan.limit_price == D("0.32")


def test_size_order_kelly_bound():
    # p=0.45 at ~0.315 cost: f* ~ 0.197; 0.1 Kelly on $1000 -> ~$19.7 -> ~62 contracts, but only 30 qualify
    plan, why = size_order(D("0.45"), ASKS, KFEE, **kw())
    assert why == "ok" and plan.contracts == 30
    assert plan.edge_per_contract >= D("0.05")


def test_size_order_respects_cost_cap_and_max_contracts():
    plan, _ = size_order(D("0.45"), ASKS, KFEE, **kw(max_cost=D(5)))
    assert plan.cost <= D("5.5") and plan.contracts <= 16
    plan, _ = size_order(D("0.45"), ASKS, KFEE, **kw(max_contracts=D(7)))
    assert plan.contracts == 7


def test_size_order_min_book_depth():
    plan, why = size_order(D("0.45"), [Level(D("0.30"), D(3))], KFEE, **kw())
    assert plan is None and "only 3" in why


def test_size_order_no_edge():
    plan, why = size_order(D("0.33"), ASKS, KFEE, **kw())
    assert plan is None and why == "no edge at best ask"


def test_fee_rounding_can_kill_tiny_orders():
    # 1 contract at 0.40 with p=0.468: un-rounded fee 0.0168 leaves edge 0.051 (qualifies), but Kalshi
    # rounds a 1-lot fee up to $0.02 -> edge 0.048 < 0.05
    plan, why = size_order(D("0.468"), [Level(D("0.40"), D(50))], KFEE,
                           **kw(min_book_contracts=D(1), max_contracts=D(1)))
    assert plan is None and "after exact fees" in why


def test_polymarket_fee_dispatch():
    assert taker_fee("polymarket_us", D(1000), D("0.50"), {"theta": 0.0695}) == D("17.38")
    assert taker_fee("kalshi", D(100), D("0.55"), {"fee_type": "quadratic", "fee_multiplier": 1}) == D("1.74")
