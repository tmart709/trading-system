"""Polymarket US client, built on the official `polymarket-us` SDK (NOT the global CLOB / py-clob-client).

Docs: https://docs.polymarket.us  (verified 2026-10-12)
  Public market data:  https://gateway.polymarket.us   (no key)
  Authenticated API:   https://api.polymarket.us       (X-PM-Access-Key / X-PM-Timestamp / X-PM-Signature,
                       Ed25519 over f"{timestamp_ms}{METHOD}{path}", timestamp within 30s of server time)
  No sandbox/test environment exists.

Market structure: one instrument per market. Buying it = YES; shorting it = NO.
The book is bids/offers on that instrument, so it maps directly to YES bids/asks.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_us import (
    APIConnectionError,
    APITimeoutError,
    AsyncPolymarketUS,
    InternalServerError,
    NotFoundError,
    RateLimitError,
)
from polymarket_us.auth import create_auth_headers

from core.exchanges.base import Exchange, ExchangeError
from core.models import Bracket, Level, Market, OrderBook, Venue
from core.ratelimit import TokenBucket, backoff_delay

log = logging.getLogger(__name__)

RATE_LIMIT_PENALTY_S = 11.0

# Bracket wording observed in Polymarket US temperature contracts. Slugs are NOT reliable:
# "...-gte71lt72f" is described as "between 71F and 72F" (inclusive), so parse the description.
_BETWEEN = re.compile(r"between (-?\d+)\s*°?F and (-?\d+)\s*°?F", re.I)
_LTE = re.compile(r"less than or equal to (-?\d+)\s*°?F", re.I)
_GTE = re.compile(r"greater than or equal to (-?\d+)\s*°?F", re.I)
_LT = re.compile(r"(?:strictly )?less than (-?\d+)\s*°?F", re.I)
_GT = re.compile(r"(?:strictly )?greater than (-?\d+)\s*°?F", re.I)
_VERIFIED_FROM = re.compile(r"Outcome verified from (.+?)\.?\s*$", re.I)
_REPORTED_BY = re.compile(r"as reported by (?:the )?(.+?)(?:\s+be\s|\?|\.|$)", re.I)


def parse_bracket(description: str) -> Bracket | None:
    if m := _BETWEEN.search(description):
        return Bracket(int(m[1]), int(m[2]))
    if m := _LTE.search(description):
        return Bracket(None, int(m[1]))
    if m := _GTE.search(description):
        return Bracket(int(m[1]), None)
    if m := _LT.search(description):
        return Bracket(None, int(m[1]) - 1)
    if m := _GT.search(description):
        return Bracket(int(m[1]) + 1, None)
    return None


def _ts(x: str | None) -> datetime | None:
    if not x:
        return None
    return datetime.fromisoformat(x.replace("Z", "+00:00"))


def _px(q: dict | None) -> Decimal | None:
    return Decimal(q["value"]) if q and q.get("value") not in (None, "") else None


def parse_book(slug: str, payload: dict, fetched_at: datetime) -> OrderBook:
    md = payload.get("marketData") or {}
    bids = [Level(_px(l["px"]), Decimal(l["qty"])) for l in md.get("bids") or []]
    asks = [Level(_px(l["px"]), Decimal(l["qty"])) for l in md.get("offers") or []]
    bids.sort(key=lambda l: l.price, reverse=True)
    asks.sort(key=lambda l: l.price)
    return OrderBook(Venue.POLYMARKET_US, slug, bids, asks, fetched_at, _ts(md.get("transactTime")))


class PolymarketUSClient(Exchange):
    venue = Venue.POLYMARKET_US

    def __init__(self, key_id: str | None = None, secret_key: str | None = None,
                 max_rps: float = 2.0, book_rps: float = 0.35, max_retries: int = 4, timeout: float = 20.0):
        self.key_id = key_id
        self._secret = secret_key
        self.max_retries = max_retries
        self._bucket = TokenBucket(max_rps)
        # /markets/{slug}/book and /bbo are limited much harder than listings: measured 2026-10-12 at
        # ~5 requests per 10s per IP, then 429 with Retry-After: 10. Keep a separate, slow bucket.
        self._book_bucket = TokenBucket(book_rps, capacity=3)
        # Disable SDK-internal retries: quick re-tries during a 429 window only extend the penalty.
        self._sdk = AsyncPolymarketUS(key_id=key_id, secret_key=secret_key, timeout=timeout, max_retries=0)

    async def aclose(self) -> None:
        await self._sdk.close()

    async def _call(self, fn, *args, bucket: TokenBucket | None = None):
        bucket = bucket or self._bucket
        for attempt in range(self.max_retries + 1):
            await bucket.acquire()
            try:
                return await fn(*args)
            except NotFoundError as e:
                raise ExchangeError(f"polymarket_us not found: {e}") from e
            except RateLimitError as e:
                if attempt == self.max_retries:
                    raise
                # Gateway sends Retry-After: 10 (seconds); wait it out fully.
                log.warning("polymarket_us rate limited; waiting %ss", RATE_LIMIT_PENALTY_S)
                await asyncio.sleep(RATE_LIMIT_PENALTY_S)
            except (APIConnectionError, APITimeoutError, InternalServerError) as e:
                if attempt == self.max_retries:
                    raise
                log.warning("polymarket_us %s: %s; backing off", type(e).__name__, e)
                await asyncio.sleep(backoff_delay(attempt, base=1.0))
        raise ExchangeError("polymarket_us: retries exhausted")

    # ---- public ----
    async def list_events(self, **params: Any) -> list[dict]:
        out, offset, limit = [], 0, 100
        while True:
            data = await self._call(self._sdk.events.list, dict(params, limit=limit, offset=offset))
            batch = data.get("events") or []
            out.extend(batch)
            if len(batch) < limit:
                return out
            offset += limit

    async def get_event(self, slug: str) -> dict:
        data = await self._call(self._sdk.events.retrieve_by_slug, slug)
        return data.get("event", data)

    async def get_market(self, market_id: str) -> Market:
        data = await self._call(self._sdk.markets.retrieve_by_slug, market_id)
        return self.to_market(data.get("market", data))

    async def get_orderbook(self, market_id: str) -> OrderBook:
        data = await self._call(self._sdk.markets.book, market_id, bucket=self._book_bucket)
        return parse_book(market_id, data, datetime.now(timezone.utc))

    async def get_bbo(self, market_id: str) -> dict:
        return await self._call(self._sdk.markets.bbo, market_id, bucket=self._book_bucket)

    async def get_settlement(self, market_id: str) -> Decimal | None:
        """Settlement price (0/1) or None if not settled yet. The API errors on unsettled markets."""
        await self._book_bucket.acquire()
        try:
            data = await self._sdk.markets.settlement(market_id)
        except Exception:
            return None
        val = data.get("settlement")
        return None if val is None else Decimal(str(val))

    @staticmethod
    def top_of_book(m: dict, fetched_at: datetime) -> OrderBook:
        """Best bid/ask embedded in event/market listings. Prices only; sizes are not provided."""
        bid, ask = _px(m.get("bestBidQuote")), _px(m.get("bestAskQuote"))
        return OrderBook(
            Venue.POLYMARKET_US, m["slug"],
            [Level(bid, Decimal(0))] if bid is not None else [],
            [Level(ask, Decimal(0))] if ask is not None else [],
            fetched_at, depth="top",
        )

    # ---- auth ----
    def auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Exposed for tests and for raw calls the SDK doesn't cover. Signing is the SDK's own."""
        if not (self.key_id and self._secret):
            raise ExchangeError("polymarket_us credentials not configured")
        return create_auth_headers(self.key_id, self._secret, method, path)

    async def get_balance(self) -> dict:
        return await self._call(self._sdk.account.balances)

    @staticmethod
    def to_market(m: dict, event: dict | None = None) -> Market:
        desc = m.get("description") or ""
        src = _VERIFIED_FROM.search(desc) or _REPORTED_BY.search(desc)
        return Market(
            venue=Venue.POLYMARKET_US,
            market_id=m["slug"],
            event_id=(event or {}).get("slug", ""),
            series_id=(event or {}).get("seriesSlug"),
            title=m.get("question") or m.get("title", ""),
            rules=desc,
            settlement_source=src[1].strip() if src else "",
            status=m.get("status", ""),
            close_time=_ts(m.get("endDate")),
            bracket=parse_bracket(desc),
            raw=m,
        )
