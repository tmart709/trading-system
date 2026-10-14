"""Paper execution: simulate an immediate-or-cancel buy against the current book. No venue calls.

Optimistic in one way (assumes our order doesn't race anyone between the book snapshot and the fill) and
conservative in another (fee on the VWAP, which is >= per-level fees). Every order and fill is logged
exactly as a real one would be, with mode='paper'.
"""

from __future__ import annotations

import json
from decimal import Decimal

from core.db import Database, utcnow
from core.models import OrderBook, Side
from core.sizing import FeeFn


class PaperBroker:
    mode = "paper"

    def __init__(self, db: Database):
        self.db = db

    def execute(self, *, client_order_id: str, strategy: str, venue: str, market_id: str, side: str,
                contracts: Decimal, limit_price: Decimal, book: OrderBook, fee_fn: FeeFn,
                signal_id: int | None = None) -> dict:
        existing = self.db.query("SELECT id, status FROM orders WHERE client_order_id=?", (client_order_id,))
        if existing:   # idempotent: the same intent is never executed twice
            return {"order_id": existing[0]["id"], "status": existing[0]["status"], "duplicate": True}

        fills, remaining = [], contracts
        for lvl in book.asks(Side(side)):
            if lvl.price > limit_price or remaining <= 0:
                break
            take = min(remaining, lvl.size)
            fills.append((take, lvl.price))
            remaining -= take
        filled = contracts - remaining
        status = "filled" if remaining == 0 else ("partial" if filled > 0 else "unfilled")
        vwap = sum((q * p for q, p in fills), Decimal(0)) / filled if filled else Decimal(0)
        total_fee = fee_fn(filled, vwap) if filled else Decimal(0)

        now = utcnow()
        with self.db.conn:
            cur = self.db.conn.execute(
                """INSERT INTO orders (client_order_id, ts, strategy, venue, market_id, side, contracts,
                       limit_price, mode, status, signal_id, details_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (client_order_id, now, strategy, venue, market_id, side, str(contracts), str(limit_price),
                 self.mode, status, signal_id,
                 json.dumps({"tif": "ioc", "filled": str(filled), "vwap": str(vwap), "fee": str(total_fee),
                             "book_as_of": book.fetched_at.isoformat()})))
            order_id = cur.lastrowid
            fee_left = total_fee
            for i, (q, p) in enumerate(fills):
                fee = total_fee * q / filled if i < len(fills) - 1 else fee_left
                fee = fee.quantize(Decimal("0.000001"))
                fee_left -= fee
                self.db.conn.execute(
                    """INSERT INTO fills (order_id, venue_fill_id, ts, venue, market_id, side, contracts,
                           price, fee, mode) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (order_id, f"{client_order_id}-{i}", now, venue, market_id, side, str(q), str(p),
                     str(fee), self.mode))
        return {"order_id": order_id, "status": status, "filled": filled, "vwap": vwap, "fee": total_fee,
                "duplicate": False}
