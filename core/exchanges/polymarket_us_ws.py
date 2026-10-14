"""Polymarket US markets WebSocket (authenticated): live full order books.

wss://api.polymarket.us/v1/ws/markets, signed handshake (same Ed25519 headers as REST, path /v1/ws/markets).
Each `marketData` message carries the market's FULL book (not a delta), so we just keep the latest per slug.
Quiet markets send nothing; a book stays current as long as the connection is alive, so freshness is
judged by connection liveness (`last_alive`), not by the per-market message time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from polymarket_us.auth import create_auth_headers
from websockets.asyncio.client import connect

from core.exchanges.polymarket_us import parse_book
from core.models import OrderBook
from core.ratelimit import backoff_delay

log = logging.getLogger(__name__)

URL = "wss://api.polymarket.us/v1/ws/markets"
PATH = "/v1/ws/markets"
SUBSCRIBE_CHUNK = 50
LIVENESS_TIMEOUT_S = 60.0   # no message or heartbeat for this long -> reconnect


class PolymarketMarketStream:
    def __init__(self, key_id: str, secret_key: str):
        self.key_id, self.secret_key = key_id, secret_key
        self.books: dict[str, OrderBook] = {}
        self.last_alive: float = 0.0          # monotonic time of last frame received
        self.connected = False
        self._wanted: set[str] = set()
        self._subscribed: set[str] = set()
        self._resubscribe = asyncio.Event()
        self.reconnects = 0

    def set_markets(self, slugs: list[str]) -> None:
        if set(slugs) != self._wanted:
            self._wanted = set(slugs)
            self._resubscribe.set()

    def healthy(self, max_silence_s: float = LIVENESS_TIMEOUT_S) -> bool:
        return self.connected and (time.monotonic() - self.last_alive) < max_silence_s

    def alive_at(self) -> datetime | None:
        """Wall-clock time up to which every received book is known to be current."""
        if not self.connected or not self.last_alive:
            return None
        return datetime.now(timezone.utc) - timedelta(seconds=time.monotonic() - self.last_alive)

    async def run(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            try:
                await self._session(stop)
                attempt = 0
            except Exception as e:
                self.connected = False
                self.reconnects += 1
                delay = backoff_delay(attempt, base=1.0, cap=60.0)
                attempt += 1
                log.warning("polymarket_us ws disconnected (%s: %s); reconnecting in %.1fs",
                            type(e).__name__, e, delay)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        self.connected = False

    async def _session(self, stop: asyncio.Event) -> None:
        headers = create_auth_headers(self.key_id, self.secret_key, "GET", PATH)
        async with connect(URL, additional_headers=headers, ping_interval=20, ping_timeout=20,
                           max_size=8 * 1024 * 1024) as ws:
            self.connected = True
            self.last_alive = time.monotonic()
            self._subscribed = set()
            self._resubscribe.set()
            log.info("polymarket_us ws connected")
            while not stop.is_set():
                if self._resubscribe.is_set():
                    self._resubscribe.clear()
                    await self._sync_subscriptions(ws)
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    if time.monotonic() - self.last_alive > LIVENESS_TIMEOUT_S:
                        raise ConnectionError("no frames for %.0fs" % LIVENESS_TIMEOUT_S)
                    continue
                self.last_alive = time.monotonic()
                self._handle(raw)
            await ws.close()

    async def _sync_subscriptions(self, ws) -> None:
        # Simplest correct approach on change: subscribe to anything new. Markets that disappear just
        # stop being read (they close/settle and go quiet); a reconnect clears them.
        new = sorted(self._wanted - self._subscribed)
        for i in range(0, len(new), SUBSCRIBE_CHUNK):
            chunk = new[i : i + SUBSCRIBE_CHUNK]
            await ws.send(json.dumps({"subscribe": {
                "requestId": f"md-{uuid.uuid4().hex[:12]}",
                "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                "marketSlugs": chunk,
            }}))
            self._subscribed.update(chunk)
        if new:
            log.info("polymarket_us ws subscribed to %d new markets (%d total)", len(new), len(self._subscribed))

    def _handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("polymarket_us ws: unparseable frame")
            return
        if "error" in msg:
            log.warning("polymarket_us ws error: %s", msg)
            return
        md = msg.get("marketData")
        if md and md.get("marketSlug"):
            book = parse_book(md["marketSlug"], msg, datetime.now(timezone.utc))
            self.books[md["marketSlug"]] = book

