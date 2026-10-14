"""Daily summary: signals, trades, PnL by strategy/venue, model calibration, settlement-source agreement."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from core.db import Database
from core.execution.portfolio import positions, realized_between
from core.marketdata import MarketData
from core.matching.weather import kalshi_event_date
from core.models import Bracket, Side, Venue


def _bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(tz))
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def calibration_table(db: Database, since: datetime | None = None) -> dict:
    """Reliability of logged model probabilities vs settled outcomes, and Brier: model vs market mid."""
    rows = db.query(
        """SELECT p.model_prob, p.market_mid, m.result FROM model_predictions p
           JOIN markets m ON m.venue = p.venue AND m.market_id = p.market_id
           WHERE m.result IN ('yes','no') AND (? IS NULL OR p.ts >= ?)""",
        (since.isoformat() if since else None, since.isoformat() if since else None))
    buckets = defaultdict(lambda: [0, 0.0, 0])
    brier_model = brier_mkt = 0.0
    n_mid = 0
    for r in rows:
        y = 1 if r["result"] == "yes" else 0
        p = r["model_prob"]
        b = min(int(p * 10), 9)
        buckets[b][0] += 1
        buckets[b][1] += p
        buckets[b][2] += y
        brier_model += (p - y) ** 2
        if r["market_mid"] is not None:
            brier_mkt += (r["market_mid"] - y) ** 2
            n_mid += 1
    n = len(rows)
    return {
        "n": n,
        "brier_model": brier_model / n if n else None,
        "brier_market_mid": brier_mkt / n_mid if n_mid else None,
        "buckets": {f"{b*10}-{b*10+10}%": (c, s / c, h / c) for b, (c, s, h) in sorted(buckets.items())},
    }


def signal_accuracy(db: Database, mode: str = "paper", model_version: str | None = None) -> dict:
    """Every market-side the model ever flagged (first flag only), scored against the settled result.

    Groups: traded / suspect (edge too big, skipped) / skipped for another reason (thin book, fees, ...).
    Hypothetical P&L assumes 1 contract bought at the first flagged ask plus that moment's fee.
    """
    rows = db.query(
        """SELECT s.venue, s.market_id, s.side, s.model_prob, s.market_price, s.fee_per_contract, s.acted,
                  s.details_json, s.ts, m.result
           FROM signals s JOIN markets m ON m.venue = s.venue AND m.market_id = s.market_id
           WHERE s.mode = ? AND (? IS NULL OR COALESCE(json_extract(s.details_json, '$.model_version'), 'v1') = ?)
           ORDER BY s.ts""", (mode, model_version, model_version))
    first: dict[tuple, dict] = {}
    for r in rows:
        key = (r["venue"], r["market_id"], r["side"])
        f = first.setdefault(key, {"row": r, "acted": False, "suspect": False})
        f["acted"] |= bool(r["acted"])
        f["suspect"] |= '"plan": "suspect' in (r["details_json"] or "")
    groups: dict[str, list] = defaultdict(list)
    for f in first.values():
        r = f["row"]
        if r["result"] not in ("yes", "no"):
            continue
        g = "traded" if f["acted"] else ("suspect" if f["suspect"] else "skipped (other)")
        won = 1 if r["result"] == r["side"] else 0
        cost = float(r["market_price"]) + float(r["fee_per_contract"] or 0)
        groups[g].append((float(r["model_prob"]), cost, won, r["venue"]))
    out = {}
    for g, xs in groups.items():
        n = len(xs)
        out[g] = {
            "n": n,
            "model_expected_win_rate": sum(x[0] for x in xs) / n,
            "price_implied_win_rate": sum(x[1] for x in xs) / n,
            "actual_win_rate": sum(x[2] for x in xs) / n,
            "pnl_per_contract": sum(x[2] - x[1] for x in xs) / n,
            "by_venue": {v: sum(1 for x in xs if x[3] == v) for v in {x[3] for x in xs}},
        }
    pending = sum(1 for f in first.values() if f["row"]["result"] not in ("yes", "no"))
    return {"groups": out, "pending": pending}


def settlement_agreement(db: Database, series_to_city: dict[str, tuple[str, str]]) -> dict:
    """Kalshi (The Weather Company) results vs the NWS CLI high for the same city/day.
    A Kalshi bracket that resolved differently from what the CLI high implies = sources disagreed."""
    rows = db.query(
        """SELECT m.market_id, m.event_id, m.series_id, m.bracket_low, m.bracket_high, m.result
           FROM markets m WHERE m.venue='kalshi' AND m.result IN ('yes','no')""")
    actuals = {}
    for r in db.query("SELECT * FROM cli_actuals"):
        actuals[(r["city"], "high", r["target_date"])] = r["high_f"]
        actuals[(r["city"], "low", r["target_date"])] = r["low_f"]
    checked = mismatched = 0
    examples = []
    for r in rows:
        city, variable = series_to_city.get(r["series_id"], (None, None))
        d = kalshi_event_date(r["event_id"])
        hi = actuals.get((city, variable, d.isoformat())) if (city and d) else None
        if hi is None:
            continue
        expected = "yes" if Bracket(r["bracket_low"], r["bracket_high"]).contains(hi) else "no"
        checked += 1
        if expected != r["result"]:
            mismatched += 1
            examples.append(f"{r['market_id']}: Kalshi={r['result']} but CLI {variable} {hi}F implies {expected}")
    return {"checked": checked, "mismatched": mismatched, "examples": examples[:5]}


def daily_report(db: Database, md: MarketData, day: date, series_to_city: dict[str, tuple[str, str]],
                 tz: str = "America/New_York", mode: str = "paper", write_pnl: bool = True) -> str:
    start, end = _bounds(day, tz)
    lines = [f"Daily report {day.isoformat()} ({tz}), mode={mode}"]

    all_sig = db.query("SELECT venue, market_id, side, acted, details_json FROM signals WHERE mode=? AND ts>=? AND ts<?",
                       (mode, start.isoformat(), end.isoformat()))
    suspect = [r for r in all_sig if '"plan": "suspect' in (r["details_json"] or "")]
    sig = [r for r in all_sig if r not in suspect]
    distinct = {(r["venue"], r["market_id"], r["side"]) for r in sig}
    by_venue = defaultdict(int)
    for v, _, _ in distinct:
        by_venue[v] += 1
    lines.append(f"Signals: {len(sig)} evaluations over threshold, {len(distinct)} distinct market-sides "
                 f"({', '.join(f'{v}: {n}' for v, n in sorted(by_venue.items())) or 'none'}), "
                 f"{sum(r['acted'] for r in sig)} acted on")
    if suspect:
        lines.append(f"Suspect signals (edge too large to trust, not traded): "
                     f"{len({(r['venue'], r['market_id'], r['side']) for r in suspect})} distinct market-sides")

    fills = db.query(
        """SELECT o.strategy, f.venue, f.contracts, f.price, f.fee FROM fills f JOIN orders o ON o.id=f.order_id
           WHERE f.mode=? AND f.ts>=? AND f.ts<?""", (mode, start.isoformat(), end.isoformat()))
    agg = defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])
    for r in fills:
        a = agg[(r["strategy"], r["venue"])]
        c = Decimal(r["contracts"])
        a[0] += c
        a[1] += c * Decimal(r["price"]) + Decimal(r["fee"])
        a[2] += Decimal(r["fee"])
    lines.append("Trades:" + ("" if agg else " none"))
    for (s, v), (c, cost, fee) in sorted(agg.items()):
        lines.append(f"  {s} / {v}: {c} contracts, cost ${cost:.2f} (fees ${fee:.2f})")

    realized = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for p in realized_between(db, mode, start, end):
        realized[(p.strategy, p.venue)][0] += p.realized_pnl
    unreal = defaultdict(Decimal)
    open_cost = defaultdict(Decimal)
    for p in positions(db, mode):
        if p.settled:
            continue
        got = md.get(Venue(p.venue), p.market_id)
        bid = got[0].best_bid(Side(p.side)) if got else None
        value = p.contracts * bid.price if bid else (p.cost if not got else Decimal(0))
        unreal[(p.strategy, p.venue)] += value - p.cost
        open_cost[(p.strategy, p.venue)] += p.cost
    keys = sorted(set(realized) | set(unreal) | set(agg))
    lines.append("PnL:" + ("" if keys else " no positions"))
    for k in keys:
        r = realized[k][0]
        u = unreal.get(k, Decimal(0))
        fee = agg[k][2] if k in agg else Decimal(0)
        lines.append(f"  {k[0]} / {k[1]}: realized ${r:+.2f}, open MTM ${u:+.2f} on ${open_cost.get(k, 0):.2f} at risk")
        if write_pnl:
            db.conn.execute(
                """INSERT INTO daily_pnl (day, strategy, venue, mode, realized, unrealized, fees) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT (day, strategy, venue, mode) DO UPDATE SET realized=excluded.realized,
                   unrealized=excluded.unrealized, fees=excluded.fees""",
                (day.isoformat(), k[0], k[1], mode, str(r), str(u), str(fee)))

    cal = calibration_table(db)
    lines.append(f"Model calibration (all settled predictions, n={cal['n']}):")
    if cal["n"]:
        lines.append(f"  Brier: model {cal['brier_model']:.4f} vs market mid "
                     f"{cal['brier_market_mid']:.4f}" if cal["brier_market_mid"] is not None else
                     f"  Brier: model {cal['brier_model']:.4f}")
        for k, (c, p, h) in cal["buckets"].items():
            lines.append(f"  predicted {k:>8}: n={c:5d}  avg {p:.3f}  happened {h:.3f}")
    acc = signal_accuracy(db, mode)
    lines.append(f"Signal accuracy (every market-side the model flagged, all model versions; {acc['pending']} not settled yet):")
    if not acc["groups"]:
        lines.append("  no settled signals yet")
    for g in ("traded", "suspect", "skipped (other)"):
        a = acc["groups"].get(g)
        if a:
            lines.append(f"  {g:16s} n={a['n']:4d}  won {a['actual_win_rate']:.1%}  model expected "
                         f"{a['model_expected_win_rate']:.1%}  price implied {a['price_implied_win_rate']:.1%}  "
                         f"P&L/contract {a['pnl_per_contract']:+.3f}")
    for version in ("v1", "v2"):
        g = signal_accuracy(db, mode, version)["groups"].get("traded")
        if g:
            lines.append(f"  model {version} bets: n={g['n']}  won {g['actual_win_rate']:.1%}  model expected "
                         f"{g['model_expected_win_rate']:.1%}  P&L/contract {g['pnl_per_contract']:+.3f}")
    agree = settlement_agreement(db, series_to_city)
    lines.append(f"Settlement sources (Kalshi/Weather Company vs NWS CLI): {agree['mismatched']} of "
                 f"{agree['checked']} settled Kalshi brackets disagreed")
    lines += [f"  {e}" for e in agree["examples"]]
    return "\n".join(lines)
