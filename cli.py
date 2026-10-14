"""Command-line entry point.

  python cli.py run                       recorder + strategies (paper trading per config modes)
  python cli.py record [--once]           data recorder only, no strategies
  python cli.py report [--date YYYY-MM-DD] daily summary (default: yesterday)
  python cli.py kill [--reason TEXT]      kill switch ON: blocks all new orders, cancels open orders
  python cli.py unkill                    kill switch off
  python cli.py calibrate                 (re)build the weather model's historical calibration set
  python cli.py dashboard [--port 8050]   live read-only dashboard at http://localhost:8050
  python cli.py status                    what's in the DB + latest prices side by side
  python cli.py pairs list [--status S]   cross-venue pairs awaiting review
  python cli.py pairs show ID             full rules text from both venues
  python cli.py pairs approve ID [--notes TEXT] / pairs reject ID [--notes TEXT]
  python cli.py check-keys                read-only credential check (balance calls); places no orders
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from datetime import date, timedelta

from core.config import env, load_config
from core.data.calibration import build_calibration_set, evaluate
from core.data.forecasts import WeatherSources
from core.db import Database
from core.engine import Engine
from core.exchanges.kalshi import KalshiClient, KalshiSigner
from core.exchanges.polymarket_us import PolymarketUSClient
from core.risk.manager import RiskManager


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def cmd_engine(cfg, once: bool, with_strategies: bool) -> None:
    eng = Engine(cfg, with_strategies=with_strategies)
    if with_strategies and eng.risk.kill_switch_on():
        print("NOTE: kill switch is ON; strategies will run but every order is blocked. `cli.py unkill` to clear.")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await eng.recorder.run(once=once, stop=stop)
    finally:
        await eng.aclose()


async def cmd_kill(cfg, reason: str) -> None:
    db = Database(cfg.db_path)
    RiskManager(cfg.risk, db).set_kill_switch(True, reason)
    print("Kill switch ON. All new orders are blocked (paper and real).")
    # Paper orders are immediate-or-cancel, so nothing rests. Real venues: cancel everything we can.
    for name, client in (("kalshi", _kalshi_trading_client()), ("polymarket_us", _pm_trading_client())):
        if client is None:
            print(f"  {name}: no trading credentials configured; nothing to cancel")
            continue
        try:
            n = await client.cancel_all_orders()
            print(f"  {name}: cancelled {n} open orders")
        except Exception as e:
            print(f"  {name}: {type(e).__name__}: {e}")
        finally:
            await client.aclose()
    db.close()


def _kalshi_trading_client():
    # Order entry isn't built yet; when it is, this returns the client for the configured trading env.
    return None


def _pm_trading_client():
    return None


async def cmd_calibrate(cfg) -> None:
    db = Database(cfg.db_path)
    w = WeatherSources(env(cfg.weather["nws_user_agent_env"], ""), [])
    try:
        print(await build_calibration_set(cfg, db, w))
    finally:
        await w.aclose()
    for code, city in cfg.cities.items():
        for variable in ("high", "low"):
            for lag in (1, 2, 3):
                r = evaluate(db, city, lag, variable=variable)
                if r["n"]:
                    print(f"{code} {variable:4s} lag{lag}: n={r['n']} MAE={r['mae']:.2f}F 80%-coverage={r['coverage_80']:.2f} "
                          f"bias now {r['current_bias']:+.2f} sigma {r['current_sigma']:.2f}")
    db.close()


def cmd_status(cfg) -> None:
    db = Database(cfg.db_path)
    print("== row counts")
    for t in ("markets", "market_rules_history", "orderbook_snapshots", "forecasts", "observations",
              "market_pairs", "system_events"):
        print(f"  {t:22s} {db.query(f'SELECT COUNT(*) c FROM {t}')[0]['c']}")
    print("\n== latest book per market (YES bid/ask, size)")
    rows = db.query("""
        SELECT s.venue, s.market_id, s.ts, s.best_yes_bid, s.best_yes_bid_size, s.best_yes_ask, s.best_yes_ask_size,
               m.bracket_low, m.bracket_high, m.event_id
        FROM orderbook_snapshots s JOIN markets m USING (venue, market_id)
        WHERE s.id IN (SELECT MAX(id) FROM orderbook_snapshots GROUP BY venue, market_id)
        ORDER BY m.event_id, s.venue, COALESCE(m.bracket_low, -999)""")
    for r in rows:
        br = f"{r['bracket_low'] if r['bracket_low'] is not None else '':>3}-{r['bracket_high'] if r['bracket_high'] is not None else '':<3}"
        print(f"  {r['venue']:14s} {r['market_id']:42s} {br} bid {r['best_yes_bid'] or '-':>6} x{r['best_yes_bid_size'] or '-':>8}"
              f"  ask {r['best_yes_ask'] or '-':>6} x{r['best_yes_ask_size'] or '-':>8}  @ {r['ts'][11:19]}Z")
    print("\n== latest forecasts")
    for r in db.query("""
        SELECT city, model, target_date, ROUND(AVG(point_f),1) p, MAX(members_json IS NOT NULL) ens FROM forecasts f
        WHERE fetched_at = (SELECT MAX(fetched_at) FROM forecasts g WHERE g.city = f.city)
          AND target_date >= DATE('now', '-1 day')
        GROUP BY city, model, target_date ORDER BY city, target_date, model"""):
        print(f"  {r['city']:4s} {r['target_date']} {r['model']:14s} {r['p']}F{'  (ensemble mean)' if r['ens'] else ''}")
    db.close()


def cmd_pairs(cfg, args) -> None:
    db = Database(cfg.db_path)
    if args.action == "list":
        rows = db.query("SELECT * FROM market_pairs WHERE (? IS NULL OR status=?) ORDER BY id",
                        (args.status, args.status))
        for r in rows:
            warn = " ⚠ " + r["warnings"].split("\n")[0][:90] if r["warnings"] else ""
            print(f"#{r['id']:<4} [{r['status']}] {r['kalshi_market_id']:28s} <-> {r['pm_market_id']:42s} {r['match_reason']}{warn}")
        print(f"{len(rows)} pair(s)")
    elif args.action == "show":
        r = db.query("SELECT * FROM market_pairs WHERE id=?", (args.id,))[0]
        print(f"Pair #{r['id']}  status={r['status']}  {r['match_reason']}\n")
        print(f"--- Kalshi {r['kalshi_market_id']}  (source: {r['kalshi_source']})\n{r['kalshi_rules']}\n")
        print(f"--- Polymarket US {r['pm_market_id']}  (source: {r['pm_source']})\n{r['pm_rules']}\n")
        print(f"--- Warnings\n{r['warnings'] or '(none)'}")
    elif args.action in ("approve", "reject"):
        db.set_pair_status(args.id, "approved" if args.action == "approve" else "rejected", args.notes)
        print(f"pair #{args.id} -> {args.action}d")
    db.close()


async def cmd_check_keys(cfg) -> None:
    for env_name in ("demo", "prod"):
        key_id = env(f"KALSHI_{env_name.upper()}_API_KEY_ID")
        key_path = env(f"KALSHI_{env_name.upper()}_PRIVATE_KEY_PATH")
        if not key_id:
            print(f"kalshi {env_name}: no key configured")
            continue
        signer = KalshiSigner.from_file(key_id, key_path)
        c = KalshiClient(env_name, signer=signer)
        try:
            bal = await c.get_balance()
            print(f"kalshi {env_name}: OK ({signer.key_type} key) balance={bal}")
        except Exception as e:
            print(f"kalshi {env_name}: FAILED {e}")
        finally:
            await c.aclose()
    if env("POLYMARKET_KEY_ID"):
        c = PolymarketUSClient(env("POLYMARKET_KEY_ID"), env("POLYMARKET_SECRET_KEY"))
        try:
            print(f"polymarket_us: OK balances={await c.get_balance()}")
        except Exception as e:
            print(f"polymarket_us: FAILED {e}")
        finally:
            await c.aclose()
    else:
        print("polymarket_us: no key configured")


def main() -> None:
    ap = argparse.ArgumentParser(prog="cli.py")
    ap.add_argument("--config", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--once", action="store_true", help="run every job once and exit")
    ru = sub.add_parser("run")
    ru.add_argument("--once", action="store_true", help="run every job (and strategies) once and exit")
    rp = sub.add_parser("report")
    rp.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    k = sub.add_parser("kill")
    k.add_argument("--reason", default="manual")
    sub.add_parser("unkill")
    sub.add_parser("calibrate")
    dsh = sub.add_parser("dashboard")
    dsh.add_argument("--port", type=int, default=8050)
    sub.add_parser("status")
    sub.add_parser("check-keys")
    p = sub.add_parser("pairs")
    p.add_argument("action", choices=["list", "show", "approve", "reject"])
    p.add_argument("id", nargs="?", type=int)
    p.add_argument("--status", choices=["pending", "approved", "rejected"])
    p.add_argument("--notes")
    args = ap.parse_args()

    setup_logging(args.verbose)
    cfg = load_config(args.config)
    if args.cmd == "record":
        asyncio.run(cmd_engine(cfg, args.once, with_strategies=False))
    elif args.cmd == "run":
        asyncio.run(cmd_engine(cfg, args.once, with_strategies=True))
    elif args.cmd == "report":
        day = date.fromisoformat(args.date) if args.date else date.today() - timedelta(days=1)
        eng = Engine(cfg, with_strategies=False)
        print(eng.report(day))
        asyncio.run(eng.aclose())
    elif args.cmd == "kill":
        asyncio.run(cmd_kill(cfg, args.reason))
    elif args.cmd == "unkill":
        db = Database(cfg.db_path)
        RiskManager(cfg.risk, db).set_kill_switch(False, "manual")
        print("Kill switch off.")
    elif args.cmd == "calibrate":
        asyncio.run(cmd_calibrate(cfg))
    elif args.cmd == "dashboard":
        from core.dashboard.server import serve
        serve(cfg, args.port)
    elif args.cmd == "status":
        cmd_status(cfg)
    elif args.cmd == "pairs":
        if args.action != "list" and args.id is None:
            ap.error("pairs show/approve/reject need an ID")
        cmd_pairs(cfg, args)
    elif args.cmd == "check-keys":
        asyncio.run(cmd_check_keys(cfg))


if __name__ == "__main__":
    main()
