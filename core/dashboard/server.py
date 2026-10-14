"""Read-only local dashboard: `python cli.py dashboard` then open http://localhost:8050

Runs as its own process and opens the SQLite file read-only, so it can never interfere with the bot.
One JSON endpoint (/api/state) feeds a single static page that refreshes itself.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.config import Config
from core.execution.portfolio import positions
from core.reporting import calibration_table, signal_accuracy

log = logging.getLogger("dashboard")

PAGE = Path(__file__).with_name("index.html")
CITY_NAMES: dict[str, str] = {}   # filled from config at startup


class ReadOnlyDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        self.conn.row_factory = sqlite3.Row

    def query(self, sql, params=()):
        return self.conn.execute(sql, tuple(params)).fetchall()

    def close(self):
        self.conn.close()


def _f(x) -> float | None:
    return None if x is None else float(x)


def _age_s(ts: str | None, now: datetime) -> float | None:
    return None if not ts else (now - datetime.fromisoformat(ts)).total_seconds()


def _event_parts(event_key: str | None) -> tuple[str | None, str | None, str | None]:
    """weather:NYC:high:2026-10-13 -> (NYC, high, 2026-10-13). Older keys without the variable are highs."""
    if event_key and event_key.startswith("weather:"):
        parts = event_key.split(":")
        if len(parts) == 4:
            return parts[1], parts[2], parts[3]
        return parts[1], "high", parts[-1]
    return None, None, None


def _bracket_label(lo, hi) -> str:
    if lo is None:
        return f"≤{hi}°"
    if hi is None:
        return f"≥{lo}°"
    return f"{lo}–{hi}°"


def build_state(db: ReadOnlyDB, cfg: Config) -> dict:
    now = datetime.now(timezone.utc)
    risk = cfg.raw.get("risk", {})

    # ---------- bot health ----------
    last_snap = db.query("SELECT ts FROM orderbook_snapshots ORDER BY id DESC LIMIT 1")
    last_pm = db.query("SELECT ts, depth FROM orderbook_snapshots WHERE venue='polymarket_us' ORDER BY id DESC LIMIT 1")
    last_fc = db.query("SELECT MAX(fetched_at) t FROM forecasts")
    kill = db.query("SELECT value FROM controls WHERE key='kill_switch'")
    errors = db.query("SELECT ts, component, message FROM system_events WHERE level IN ('ERROR','CRITICAL') "
                      "ORDER BY id DESC LIMIT 5")
    snap_age = _age_s(last_snap[0]["ts"], now) if last_snap else None
    bot = {
        "running": snap_age is not None and snap_age < 180,
        "last_snapshot_age_s": snap_age,
        "last_forecast_age_s": _age_s(last_fc[0]["t"], now) if last_fc and last_fc[0]["t"] else None,
        "polymarket_feed": ("live (WebSocket)" if last_pm and last_pm[0]["depth"] == "full" else "polling") if last_pm else "none",
        "kill_switch": bool(kill and kill[0]["value"] == "on"),
        "mode": {v: cfg.effective_mode("weather_fair_value", v) for v in ("kalshi", "polymarket_us")},
        "uncapped": bool(risk.get("paper_uncapped")),
        "cities": [c.code for c in cfg.strategy_cities("weather_fair_value")],
        "variables": cfg.strategy("weather_fair_value").get("variables", ["high"]),
        "recent_errors": [dict(r) for r in errors],
    }

    # ---------- latest book per open market ----------
    open_markets = db.query(
        """SELECT venue, market_id, event_key, bracket_low, bracket_high, close_time, result FROM markets
           WHERE event_key LIKE 'weather:%' AND (result IS NULL OR settled_at >= datetime('now','-3 days'))""")
    books = {}
    for m in open_markets:
        r = db.query("SELECT best_yes_bid, best_yes_ask, ts FROM orderbook_snapshots WHERE venue=? AND market_id=? "
                     "ORDER BY ts DESC LIMIT 1", (m["venue"], m["market_id"]))
        if r:
            books[(m["venue"], m["market_id"])] = dict(r[0])
    minfo = {(m["venue"], m["market_id"]): dict(m) for m in open_markets}
    # markets referenced by positions may be older than 3 days; fetch lazily
    def market(venue, mid):
        if (venue, mid) not in minfo:
            r = db.query("SELECT venue, market_id, event_key, bracket_low, bracket_high, close_time, result "
                         "FROM markets WHERE venue=? AND market_id=?", (venue, mid))
            minfo[(venue, mid)] = dict(r[0]) if r else {}
        return minfo[(venue, mid)]

    # ---------- latest model probability per market ----------
    preds = {}
    for r in db.query(
            """SELECT p.venue, p.market_id, p.model_prob, p.details_json, p.ts FROM model_predictions p
               JOIN (SELECT venue, market_id, MAX(id) mid FROM model_predictions GROUP BY venue, market_id) x
               ON x.mid = p.id"""):
        preds[(r["venue"], r["market_id"])] = (r["model_prob"], json.loads(r["details_json"] or "{}"), r["ts"])

    # ---------- positions ----------
    pos_rows, open_cost, open_value, realized = [], 0.0, 0.0, 0.0
    settled_rows = []
    for p in positions(db, "paper"):
        m = market(p.venue, p.market_id)
        city, var, day = _event_parts(m.get("event_key"))
        bk = books.get((p.venue, p.market_id))
        bid = None
        if bk:
            if p.side == "yes" and bk["best_yes_bid"] is not None:
                bid = float(bk["best_yes_bid"])
            elif p.side == "no" and bk["best_yes_ask"] is not None:
                bid = 1 - float(bk["best_yes_ask"])
        pr = preds.get((p.venue, p.market_id))
        model_side = None if not pr else (pr[0] if p.side == "yes" else 1 - pr[0])
        row = {
            "city": CITY_NAMES.get(city, city), "date": day, "variable": var,
            "bracket": _bracket_label(m.get("bracket_low"), m.get("bracket_high")),
            "venue": p.venue, "market_id": p.market_id, "side": p.side.upper(),
            "contracts": float(p.contracts), "cost": float(p.cost),
            "avg_price": float(p.cost / p.contracts) if p.contracts else None,
            "model_prob": model_side,
        }
        if p.settled:
            pnl = float(p.realized_pnl)
            realized += pnl
            settled_rows.append({**row, "result": "WON" if p.payout > 0 else "LOST", "pnl": pnl})
        else:
            value = p.contracts * Decimal(str(bid)) if bid is not None else None
            mtm = float(value - p.cost) if value is not None else None
            open_cost += float(p.cost)
            open_value += float(value) if value is not None else float(p.cost)
            pos_rows.append({**row, "bid": bid, "mtm": mtm})
    pos_rows.sort(key=lambda r: (r["date"] or "", r["city"] or "", r["bracket"]))
    settled_rows.sort(key=lambda r: (r["date"] or "", r["city"] or ""), reverse=True)

    # cumulative realized P&L by settlement time
    pnl_series, running = [], 0.0
    settle_times = {(r["venue"], r["market_id"]): r["settled_at"] for r in db.query(
        "SELECT venue, market_id, settled_at FROM markets WHERE settled_at IS NOT NULL")}
    for p in sorted((p for p in positions(db, "paper") if p.settled),
                    key=lambda p: settle_times.get((p.venue, p.market_id)) or ""):
        running += float(p.realized_pnl)
        pnl_series.append({"t": settle_times.get((p.venue, p.market_id)), "pnl": round(running, 2)})

    # ---------- model vs market, per city-day ----------
    events: dict[str, dict] = {}
    for (venue, mid), m in minfo.items():
        if not m or m.get("result") or not m.get("event_key"):
            continue
        city, var, day = _event_parts(m["event_key"])
        key = f"{city}:{var}:{day}"   # old (3-part) and new keys for the same event merge
        ev = events.setdefault(key, {"city": CITY_NAMES.get(city, city), "code": city, "date": day,
                                     "variable": var, "brackets": {}, "model": None})
        label = _bracket_label(m["bracket_low"], m["bracket_high"])
        b = ev["brackets"].setdefault(label, {"label": label, "low": m["bracket_low"], "high": m["bracket_high"],
                                              "model": None, "kalshi": None, "polymarket_us": None})
        pr = preds.get((venue, mid))
        if pr:
            b["model"] = pr[0]
            ev["model"] = {"mu": pr[1].get("mu"), "sigma": pr[1].get("sigma"), "floor": pr[1].get("floor"),
                           "centers": pr[1].get("centers"), "as_of": pr[2]}
        bk = books.get((venue, mid))
        if bk:
            b[venue] = {"bid": _f(bk["best_yes_bid"]), "ask": _f(bk["best_yes_ask"])}
    city_days = []
    for ev in events.values():
        brs = sorted(ev["brackets"].values(), key=lambda b: (b["low"] if b["low"] is not None else -999))
        city_days.append({**ev, "brackets": brs})
    city_days.sort(key=lambda e: (e["date"] or "", e["city"] or "", e["variable"] or ""))

    # ---------- activity ----------
    activity = []
    for r in db.query(
            """SELECT o.ts, o.venue, o.market_id, o.side, o.details_json, s.model_prob, s.market_price
               FROM orders o LEFT JOIN signals s ON s.id = o.signal_id WHERE o.mode='paper'
               ORDER BY o.id DESC LIMIT 30"""):
        d = json.loads(r["details_json"] or "{}")
        m = market(r["venue"], r["market_id"])
        city, var, day = _event_parts(m.get("event_key"))
        activity.append({"ts": r["ts"], "kind": "bet", "venue": r["venue"],
                         "text": f"Bought {Decimal(d.get('filled', '0')).normalize():f} {r['side'].upper()} "
                                 f"{CITY_NAMES.get(city, city)} {var or ''} {day} {_bracket_label(m.get('bracket_low'), m.get('bracket_high'))} "
                                 f"@ {float(d.get('vwap') or 0):.2f}",
                         "model": _f(r["model_prob"]), "price": _f(r["market_price"])})
    seen = set()
    for r in db.query(
            """SELECT ts, venue, market_id, side, model_prob, market_price, edge FROM signals
               WHERE details_json LIKE '%"plan": "suspect%' ORDER BY id DESC LIMIT 200"""):
        key = (r["venue"], r["market_id"], r["side"])
        if key in seen:
            continue
        seen.add(key)
        m = market(r["venue"], r["market_id"])
        city, var, day = _event_parts(m.get("event_key"))
        activity.append({"ts": r["ts"], "kind": "suspect", "venue": r["venue"],
                         "text": f"Skipped {r['side'].upper()} {CITY_NAMES.get(city, city)} {var or ''} {day} "
                                 f"{_bracket_label(m.get('bracket_low'), m.get('bracket_high'))}: edge "
                                 f"{float(r['edge']):.2f} too large to trust",
                         "model": _f(r["model_prob"]), "price": _f(r["market_price"])})
    activity.sort(key=lambda a: a["ts"], reverse=True)

    acc = signal_accuracy(db, "paper")
    cal = calibration_table(db)
    settled_n = len(settled_rows)
    wins = sum(1 for r in settled_rows if r["result"] == "WON")
    return {
        "now": now.isoformat(),
        "bot": bot,
        "kpis": {
            "open_bets": len(pos_rows), "at_risk": round(open_cost, 2),
            "open_mtm": round(open_value - open_cost, 2), "realized": round(realized, 2),
            "settled_bets": settled_n, "win_rate": wins / settled_n if settled_n else None,
            "expected_win_rate": (sum(r["model_prob"] or 0 for r in settled_rows) / settled_n) if settled_n else None,
            "suspect_skipped": len(seen),
        },
        "positions": pos_rows,
        "settled": settled_rows[:100],
        "pnl_series": pnl_series,
        "city_days": city_days,
        "accuracy": acc,
        "calibration": cal,
        "activity": activity[:40],
    }


class Handler(BaseHTTPRequestHandler):
    cfg: Config

    def log_message(self, fmt, *args):  # quiet
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if self.path.startswith("/api/state"):
            try:
                db = ReadOnlyDB(self.cfg.db_path)
                try:
                    state = build_state(db, self.cfg)
                finally:
                    db.close()
                return self._send(200, json.dumps(state, default=str).encode(), "application/json")
            except Exception as e:
                log.exception("state failed")
                return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")
        return self._send(404, b"not found", "text/plain")


def serve(cfg: Config, port: int = 8050) -> None:
    Handler.cfg = cfg
    CITY_NAMES.update({c.code: c.name or c.code for c in cfg.cities.values()})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Dashboard: http://localhost:{port}  (read-only; Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
