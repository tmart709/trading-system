from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from core.db import Database
from core.execution.paper import PaperBroker
from core.execution.portfolio import positions
from core.fees import kalshi_fee
from core.marketdata import MarketData
from core.models import Level, OrderBook, Venue
from core.risk.manager import ProposedOrder, RiskManager

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)
RISK = {
    "paper_bankroll_usd": 1000, "max_position_per_market_usd": 50, "max_exposure_per_event_usd": 100,
    "max_exposure_per_venue_usd": 300, "max_total_exposure_usd": 500, "max_new_exposure_per_day_usd": 250,
    "daily_loss_limit_usd": 100, "max_contracts_per_order": 200, "min_price": 0.02, "max_price": 0.98,
    "max_book_age_seconds": 150, "max_forecast_age_minutes": 180,
}
KFEE = lambda c, p: kalshi_fee(c, p)  # noqa: E731


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.sqlite")
    d.conn.execute("""INSERT INTO markets (venue, market_id, event_id, series_id, title, rules, status, close_time,
                      first_seen, last_seen) VALUES ('kalshi','M1','E1','S','t','r','active',?, ?, ?)""",
                   ((NOW + timedelta(hours=10)).isoformat(), NOW.isoformat(), NOW.isoformat()))
    yield d
    d.close()


def book(asks, bids=()):
    return OrderBook(Venue.KALSHI, "M1", list(bids), list(asks), NOW)


def order(**over):
    base = dict(strategy="s", venue="kalshi", market_id="M1", event_id="E1", side="yes", contracts=D(10),
                limit_price=D("0.30"), cost=D("3.15"), close_time=NOW + timedelta(hours=10))
    base.update(over)
    return ProposedOrder(**base)


def check(risk, o, **over):
    kw = dict(mode="paper", now=NOW, book_age_s=10, forecast_age_min=30, min_minutes_to_close=30)
    kw.update(over)
    return risk.check_order(o, **kw)


# ---------------- risk ----------------

def test_clean_order_passes(db):
    assert check(RiskManager(RISK, db), order()) == []


def test_kill_switch_blocks_and_clears(db):
    r = RiskManager(RISK, db)
    r.set_kill_switch(True, "test")
    assert any("kill switch" in x for x in check(r, order()))
    r.set_kill_switch(False, "test")
    assert check(r, order()) == []


def test_stale_data_blocks(db):
    r = RiskManager(RISK, db)
    assert any("order book is" in x for x in check(r, order(), book_age_s=500))
    assert any("forecast is" in x for x in check(r, order(), forecast_age_min=400))


def test_order_sanity(db):
    r = RiskManager(RISK, db)
    assert any("outside" in x for x in check(r, order(limit_price=D("0.99"))))
    assert any("max 200" in x for x in check(r, order(contracts=D(500))))
    assert any("can't profit" in x for x in check(r, order(contracts=D(1), cost=D("1.01"))))
    assert any("closes in" in x for x in check(r, order(close_time=NOW + timedelta(minutes=5))))
    assert any("close time" in x for x in check(r, order(close_time=None)))


def test_exposure_caps_and_opposite_side(db):
    r, b = RiskManager(RISK, db), PaperBroker(db)
    b.execute(client_order_id="a", strategy="s", venue="kalshi", market_id="M1", side="yes", contracts=D(100),
              limit_price=D("0.40"), book=book([Level(D("0.40"), D(100))]), fee_fn=KFEE)
    # existing ~$41.68 in M1; a $10 order breaches the $50 per-market cap
    assert any("market exposure" in x for x in check(r, order(cost=D(10))))
    assert any("opposite side" in x for x in check(r, order(side="no", cost=D(1))))
    assert r.headroom("kalshi", "M1", "E1", "paper", NOW) < D(9)


def test_daily_loss_limit_halts(db):
    r, b = RiskManager(RISK, db), PaperBroker(db)
    b.execute(client_order_id="a", strategy="s", venue="kalshi", market_id="M1", side="yes", contracts=D(200),
              limit_price=D("0.60"), book=book([Level(D("0.60"), D(200))]), fee_fn=KFEE)
    md = MarketData()
    # Market collapses: best YES bid 0.05 -> MTM loss ~ 200*0.55 + fees > $100
    md.put(OrderBook(Venue.KALSHI, "M1", [Level(D("0.05"), D(10))], [Level(D("0.06"), D(10))], NOW))
    reason = r.enforce_daily_loss("paper", md, NOW)
    assert reason and "hit limit" in reason
    assert r.halted_today(NOW)
    assert any("halted" in x for x in check(r, order(market_id="M1", cost=D(1))))
    assert not r.halted_today(NOW + timedelta(days=1))   # resets next day


# ---------------- paper execution ----------------

def test_paper_ioc_partial_fill_and_fees(db):
    b = PaperBroker(db)
    bk = book([Level(D("0.30"), D(5)), Level(D("0.31"), D(3)), Level(D("0.35"), D(50))])
    res = b.execute(client_order_id="x", strategy="s", venue="kalshi", market_id="M1", side="yes",
                    contracts=D(20), limit_price=D("0.31"), book=bk, fee_fn=KFEE)
    assert res["status"] == "partial" and res["filled"] == D(8)
    assert res["vwap"] == (D(5) * D("0.30") + D(3) * D("0.31")) / 8
    fills = db.query("SELECT fee FROM fills")
    assert sum(D(f["fee"]) for f in fills) == res["fee"] == kalshi_fee(D(8), res["vwap"])


def test_paper_no_side_uses_reciprocal_book(db):
    b = PaperBroker(db)
    # YES bids at 0.70 -> NO asks at 0.30
    bk = OrderBook(Venue.KALSHI, "M1", [Level(D("0.70"), D(10))], [Level(D("0.72"), D(10))], NOW)
    res = b.execute(client_order_id="n", strategy="s", venue="kalshi", market_id="M1", side="no",
                    contracts=D(4), limit_price=D("0.30"), book=bk, fee_fn=KFEE)
    assert res["status"] == "filled" and res["vwap"] == D("0.30")


def test_paper_idempotent(db):
    b = PaperBroker(db)
    bk = book([Level(D("0.30"), D(100))])
    kw = dict(client_order_id="same", strategy="s", venue="kalshi", market_id="M1", side="yes",
              contracts=D(10), limit_price=D("0.30"), book=bk, fee_fn=KFEE)
    b.execute(**kw)
    assert b.execute(**kw)["duplicate"] is True
    assert len(db.query("SELECT * FROM orders")) == 1
    assert len(db.query("SELECT * FROM fills")) == 1


def test_unfilled_when_limit_below_book(db):
    res = PaperBroker(db).execute(client_order_id="u", strategy="s", venue="kalshi", market_id="M1", side="yes",
                                  contracts=D(10), limit_price=D("0.20"), book=book([Level(D("0.30"), D(100))]),
                                  fee_fn=KFEE)
    assert res["status"] == "unfilled" and db.query("SELECT COUNT(*) c FROM fills")[0]["c"] == 0


def test_settlement_pnl(db):
    PaperBroker(db).execute(client_order_id="w", strategy="s", venue="kalshi", market_id="M1", side="yes",
                            contracts=D(10), limit_price=D("0.30"), book=book([Level(D("0.30"), D(100))]), fee_fn=KFEE)
    db.conn.execute("UPDATE markets SET result='yes' WHERE market_id='M1'")
    (p,) = positions(db, "paper")
    assert p.payout == 10 and p.realized_pnl == D(10) - (D(3) + kalshi_fee(D(10), D("0.30")))
    db.conn.execute("UPDATE markets SET result='no' WHERE market_id='M1'")
    (p,) = positions(db, "paper")
    assert p.realized_pnl == -(D(3) + kalshi_fee(D(10), D("0.30")))


def test_event_cap_spans_venues(db):
    # Same real-world event listed on both venues under different venue event IDs.
    db.conn.execute("UPDATE markets SET event_key='weather:NYC:2026-10-13' WHERE market_id='M1'")
    db.conn.execute("""INSERT INTO markets (venue, market_id, event_id, event_key, title, rules, status, close_time,
                       first_seen, last_seen) VALUES ('polymarket_us','P1','temp-nychigh-2026-10-13',
                       'weather:NYC:2026-10-13','t','r','open',?,?,?)""",
                    ((NOW + timedelta(hours=10)).isoformat(), NOW.isoformat(), NOW.isoformat()))
    r, b = RiskManager(RISK, db), PaperBroker(db)
    b.execute(client_order_id="k", strategy="s", venue="kalshi", market_id="M1", side="yes", contracts=D(100),
              limit_price=D("0.45"), book=book([Level(D("0.45"), D(100))]), fee_fn=KFEE)   # ~$46.73
    b.execute(client_order_id="p", strategy="s", venue="polymarket_us", market_id="P1", side="yes",
              contracts=D(100), limit_price=D("0.45"), book=book([Level(D("0.45"), D(100))]), fee_fn=KFEE)
    reasons = check(r, order(venue="polymarket_us", market_id="P1", event_id="weather:NYC:2026-10-13", cost=D(10)))
    assert any(x.startswith("event exposure") for x in reasons)


def test_paper_uncapped_skips_exposure_caps_but_not_safety(db):
    r = RiskManager({**RISK, "paper_uncapped": True}, db)
    assert check(r, order(cost=D(62), contracts=D(200))) == []              # > $50 market cap, ignored
    assert r.headroom("kalshi", "M1", "E1", "paper", NOW) > D(10_000)
    assert any("order book is" in x for x in check(r, order(), book_age_s=999))   # stale data still blocks
    r.set_kill_switch(True, "t")
    assert any("kill switch" in x for x in check(r, order()))                       # kill switch still blocks
    # never applies outside paper
    r.set_kill_switch(False, "t")
    assert any("exposure" in x for x in check(r, order(cost=D(62), contracts=D(200)), mode="live"))


def test_signal_accuracy_groups(db):
    from core.reporting import signal_accuracy
    db.conn.execute("UPDATE markets SET result='yes' WHERE market_id='M1'")
    rows = [("yes", "0.60", "0.40", 1, '{"plan": "ok"}'),          # traded, won
            ("no", "0.30", "0.10", 0, '{"plan": "suspect: x"}')]   # suspect, lost
    for side, p, ask, acted, d in rows:
        db.conn.execute("""INSERT INTO signals (ts, strategy, venue, market_id, side, model_prob, market_price,
                           fee_per_contract, edge, size, mode, acted, details_json)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (NOW.isoformat(), "s", "kalshi", "M1", side, p, ask, "0.01", "0.1", "1", "paper", acted, d))
    g = signal_accuracy(db)["groups"]
    assert g["traded"]["n"] == 1 and g["traded"]["actual_win_rate"] == 1.0
    assert g["traded"]["pnl_per_contract"] == pytest.approx(1 - 0.41)
    assert g["suspect"]["n"] == 1 and g["suspect"]["pnl_per_contract"] == pytest.approx(-0.11)
