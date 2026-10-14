from datetime import datetime, timezone
from decimal import Decimal as D

from core.exchanges.kalshi import parse_orderbook
from core.exchanges.polymarket_us import PolymarketUSClient, parse_book
from core.models import Side

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)

# Example response from docs.kalshi.com/getting_started/orderbook_responses
KALSHI_DOC_EXAMPLE = {
    "orderbook_fp": {
        "yes_dollars": [["0.0100", "200.00"], ["0.1500", "100.00"], ["0.3500", "11.00"],
                        ["0.4100", "10.00"], ["0.4200", "13.00"]],
        "no_dollars": [["0.0100", "100.00"], ["0.4500", "20.00"], ["0.5600", "17.00"]],
    }
}


def test_kalshi_implied_asks_from_reciprocal_bids():
    ob = parse_orderbook("T", KALSHI_DOC_EXAMPLE, NOW)
    assert ob.best_bid(Side.YES).price == D("0.42")
    assert ob.best_bid(Side.YES).size == D("13")
    # Best YES ask = 1 - best NO bid = 1 - 0.56 = 0.44 (docs example), size from the NO bid.
    assert ob.best_ask(Side.YES).price == D("0.44")
    assert ob.best_ask(Side.YES).size == D("17")
    # Best NO ask = 1 - best YES bid = 0.58; best NO bid = 0.56
    assert ob.best_ask(Side.NO).price == D("0.58")
    assert ob.best_bid(Side.NO).price == D("0.56")
    # Asks sorted best (lowest) first
    assert [l.price for l in ob.yes_asks] == [D("0.44"), D("0.55"), D("0.99")]


def test_kalshi_empty_book():
    ob = parse_orderbook("T", {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}, NOW)
    assert ob.best_ask(Side.YES) is None and ob.best_bid(Side.NO) is None


def test_depth_at_or_better():
    ob = parse_orderbook("T", KALSHI_DOC_EXAMPLE, NOW)
    assert ob.depth_at_or_better(Side.YES, D("0.44")) == D("17")
    assert ob.depth_at_or_better(Side.YES, D("0.55")) == D("37")
    assert ob.depth_at_or_better(Side.NO, D("0.60")) == D("23")  # NO asks: 0.58 x13 + 0.59 x10


def test_polymarket_us_book_sorted_and_mirrored():
    payload = {"marketData": {
        "bids": [{"px": {"value": "0.1900"}, "qty": "33"}, {"px": {"value": "0.2000"}, "qty": "1"}],
        "offers": [{"px": {"value": "0.2500"}, "qty": "10"}, {"px": {"value": "0.2400"}, "qty": "84"}],
    }}
    ob = parse_book("slug", payload, NOW)
    assert ob.best_bid(Side.YES).price == D("0.20")
    assert ob.best_ask(Side.YES).price == D("0.24") and ob.best_ask(Side.YES).size == D("84")
    # Taking NO = shorting the instrument at the bid: NO costs 1 - 0.20 = 0.80
    assert ob.best_ask(Side.NO).price == D("0.80")


def test_polymarket_us_top_of_book_has_no_sizes():
    ob = PolymarketUSClient.top_of_book(
        {"slug": "s", "bestBidQuote": {"value": "0.51"}, "bestAskQuote": {"value": "0.55"}}, NOW)
    assert ob.depth == "top"
    assert ob.best_ask(Side.YES).price == D("0.55")
