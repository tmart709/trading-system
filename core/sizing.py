"""Edge and position sizing for buying a binary outcome against an order book. Pure functions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Callable

from core.models import Level

FeeFn = Callable[[Decimal, Decimal], Decimal]   # (contracts, price) -> total fee for one order


@dataclass(frozen=True)
class FillPlan:
    contracts: Decimal
    vwap: Decimal               # average price paid, before fees
    fee: Decimal                # total taker fee for the order
    limit_price: Decimal        # worst level touched (the IOC limit)
    prob: Decimal

    @property
    def cost(self) -> Decimal:
        return self.contracts * self.vwap + self.fee

    @property
    def cost_per_contract(self) -> Decimal:
        return self.cost / self.contracts

    @property
    def edge_per_contract(self) -> Decimal:
        return self.prob - self.cost_per_contract


def qualifying_depth(prob: Decimal, asks: list[Level], fee_fn: FeeFn, min_edge: Decimal) -> list[Level]:
    """Ask levels whose price plus (approximate, un-rounded) per-contract fee leaves >= min_edge."""
    out = []
    for lvl in asks:   # best (cheapest) first
        marginal_fee = fee_fn(Decimal(100), lvl.price) / 100
        if lvl.price + marginal_fee <= prob - min_edge:
            out.append(lvl)
        else:
            break
    return out


def plan_for_size(prob: Decimal, levels: list[Level], contracts: Decimal, fee_fn: FeeFn) -> FillPlan | None:
    """Walk `levels` to fill `contracts`. Fee is charged on the VWAP; because p*(1-p) is concave this is
    never lower than per-level fees, so it's conservative for multi-level fills."""
    remaining, notional, worst = contracts, Decimal(0), None
    for lvl in levels:
        take = min(remaining, lvl.size)
        if take <= 0:
            break
        notional += take * lvl.price
        remaining -= take
        worst = lvl.price
    filled = contracts - remaining
    if filled <= 0 or worst is None:
        return None
    vwap = notional / filled
    return FillPlan(filled, vwap, fee_fn(filled, vwap), worst, prob)


def kelly_fraction_binary(prob: Decimal, cost_per_contract: Decimal) -> Decimal:
    """Full-Kelly bankroll fraction for paying `cost` for a $1 payoff that wins with `prob`."""
    if cost_per_contract <= 0 or cost_per_contract >= 1 or prob <= cost_per_contract:
        return Decimal(0)
    return (prob - cost_per_contract) / (1 - cost_per_contract)


def size_order(
    prob: Decimal,
    asks: list[Level],
    fee_fn: FeeFn,
    *,
    min_edge: Decimal,
    min_book_contracts: Decimal,
    bankroll: Decimal,
    kelly_fraction: Decimal,
    max_contracts: Decimal,
    max_cost: Decimal,
) -> tuple[FillPlan | None, str]:
    """Returns (plan, reason). plan is None when there's nothing worth doing; reason says why."""
    levels = qualifying_depth(prob, asks, fee_fn, min_edge)
    available = sum((l.size for l in levels), Decimal(0)).to_integral_value(ROUND_FLOOR)
    if available <= 0:
        return None, "no edge at best ask"
    if available < min_book_contracts:
        return None, f"only {available} contracts at qualifying prices (< {min_book_contracts})"
    # Kelly on the best-level cost (approximate per-contract fee), then cap.
    unit_cost = levels[0].price + fee_fn(Decimal(100), levels[0].price) / 100
    f = kelly_fraction_binary(prob, unit_cost) * kelly_fraction
    kelly_contracts = (f * bankroll / unit_cost).to_integral_value(ROUND_FLOOR)
    cost_cap_contracts = (max_cost / unit_cost).to_integral_value(ROUND_FLOOR)
    n = min(available, kelly_contracts, max_contracts, cost_cap_contracts)
    if n <= 0:
        why = "risk caps leave no room" if kelly_contracts > 0 else "kelly size rounds to 0"
        return None, why
    plan = plan_for_size(prob, levels, n, fee_fn)
    # Fee rounding (whole cents per order) can eat the edge on small sizes; trim until it clears or give up.
    while plan and plan.edge_per_contract < min_edge and plan.contracts > 1:
        plan = plan_for_size(prob, levels, plan.contracts - 1, fee_fn)
    if not plan or plan.edge_per_contract < min_edge:
        return None, "edge below threshold after exact fees"
    return plan, "ok"
