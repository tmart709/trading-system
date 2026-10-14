"""In-memory latest-book cache shared by the recorder and strategies.

`as_of` is the time up to which a book is known to be current. Strategies compare it against the
risk layer's max data age; anything older is not tradable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.exchanges.polymarket_us_ws import PolymarketMarketStream
from core.models import OrderBook, Venue


class MarketData:
    def __init__(self, pm_stream: PolymarketMarketStream | None = None):
        self._books: dict[tuple[Venue, str], OrderBook] = {}
        self.pm_stream = pm_stream

    def put(self, book: OrderBook) -> None:
        if book.depth == "full":   # top-of-book snapshots have no sizes; never trade off them
            self._books[(book.venue, book.market_id)] = book

    def get(self, venue: Venue, market_id: str) -> tuple[OrderBook, datetime] | None:
        if venue is Venue.POLYMARKET_US and self.pm_stream and self.pm_stream.healthy():
            book = self.pm_stream.books.get(market_id)
            alive = self.pm_stream.alive_at()
            if book and alive:
                return book, alive
        book = self._books.get((venue, market_id))
        return (book, book.fetched_at) if book else None

    @staticmethod
    def age_seconds(as_of: datetime, now: datetime | None = None) -> float:
        return ((now or datetime.now(timezone.utc)) - as_of).total_seconds()
