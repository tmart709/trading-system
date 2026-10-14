"""Kalshi Trade API v2 client (public market data + request signing).

Docs: https://docs.kalshi.com  (verified 2026-10-12)
  prod REST  https://external-api.kalshi.com/trade-api/v2
  demo REST  https://external-api.demo.kalshi.co/trade-api/v2
  Signing: base64(sign(f"{timestamp_ms}{METHOD}{path_without_query}")), path includes /trade-api/v2.
           Ed25519 keys sign the message directly; RSA keys use RSA-PSS/SHA-256, salt = digest length.
           The PEM header does not reveal the key type, so it's detected from the parsed key.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.exchanges.base import Exchange, ExchangeError
from core.models import Bracket, Level, Market, OrderBook, Venue
from core.ratelimit import TokenBucket, backoff_delay

log = logging.getLogger(__name__)

BASE_URLS = {
    "prod": "https://external-api.kalshi.com/trade-api/v2",
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
}
WS_URLS = {
    "prod": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    "demo": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
}
RETRYABLE = {429, 500, 502, 503, 504}
MAX_BATCH_ORDERBOOKS = 100


class KalshiSigner:
    def __init__(self, api_key_id: str, private_key):
        if not isinstance(private_key, (Ed25519PrivateKey, rsa.RSAPrivateKey)):
            raise ValueError(f"unsupported Kalshi key type: {type(private_key).__name__}")
        self.api_key_id = api_key_id
        self.private_key = private_key

    @classmethod
    def from_pem(cls, api_key_id: str, pem: bytes) -> "KalshiSigner":
        return cls(api_key_id, serialization.load_pem_private_key(pem, password=None))

    @classmethod
    def from_file(cls, api_key_id: str, path: str | Path) -> "KalshiSigner":
        return cls.from_pem(api_key_id, Path(path).read_bytes())

    @property
    def key_type(self) -> str:
        return "ed25519" if isinstance(self.private_key, Ed25519PrivateKey) else "rsa"

    def sign(self, timestamp_ms: str, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method.upper()}{path.split('?')[0]}".encode()
        if isinstance(self.private_key, Ed25519PrivateKey):
            sig = self.private_key.sign(message)
        else:
            sig = self.private_key.sign(
                message,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return base64.b64encode(sig).decode()

    def headers(self, method: str, path: str, timestamp_ms: str | None = None) -> dict[str, str]:
        ts = timestamp_ms or str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, path),
        }


def _d(x: Any) -> Decimal | None:
    return None if x in (None, "") else Decimal(str(x))


def _ts(x: str | None) -> datetime | None:
    if not x:
        return None
    return datetime.fromisoformat(x.replace("Z", "+00:00"))


def parse_bracket(m: dict) -> Bracket | None:
    """Integer-degree bracket from Kalshi strike fields. None if strikes aren't whole numbers."""
    st, lo, hi = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")

    def whole(v):
        return v is not None and float(v) == int(float(v))

    if st == "between" and whole(lo) and whole(hi):
        return Bracket(int(float(lo)), int(float(hi)))
    if st == "less" and whole(hi):          # "less than 71" -> <= 70
        return Bracket(None, int(float(hi)) - 1)
    if st == "greater" and whole(lo):       # "greater than 78" -> >= 79
        return Bracket(int(float(lo)) + 1, None)
    return None


def parse_orderbook(ticker: str, payload: dict, fetched_at: datetime) -> OrderBook:
    """Kalshi returns YES bids and NO bids, each ascending by price. A NO bid at p is a YES ask at 1-p."""
    ob = payload.get("orderbook_fp") or {}
    yes = ob.get("yes_dollars") or []
    no = ob.get("no_dollars") or []
    yes_bids = [Level(Decimal(p), Decimal(s)) for p, s in reversed(yes)]
    yes_asks = [Level(Decimal(1) - Decimal(p), Decimal(s)) for p, s in reversed(no)]
    return OrderBook(Venue.KALSHI, ticker, yes_bids, yes_asks, fetched_at)


class KalshiClient(Exchange):
    venue = Venue.KALSHI

    def __init__(
        self,
        env: str = "prod",
        signer: KalshiSigner | None = None,
        max_rps: float = 8.0,
        timeout: float = 20.0,
        max_retries: int = 5,
    ):
        if env not in BASE_URLS:
            raise ValueError(f"env must be one of {list(BASE_URLS)}")
        self.env = env
        self.base_url = BASE_URLS[env]
        self._base_path = urlparse(self.base_url).path
        self.signer = signer
        self.max_retries = max_retries
        self._bucket = TokenBucket(max_rps)
        self._http = httpx.AsyncClient(timeout=timeout, headers={"Accept": "application/json"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, params=None, json=None, auth=False) -> dict:
        url = self.base_url + path
        for attempt in range(self.max_retries + 1):
            await self._bucket.acquire()
            headers = {}
            if auth:
                if not self.signer:
                    raise ExchangeError("authenticated Kalshi call without credentials")
                headers = self.signer.headers(method, self._base_path + path)
            try:
                r = await self._http.request(method, url, params=params, json=json, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if method != "GET" or attempt == self.max_retries:
                    raise
                log.warning("kalshi %s %s transport error %s; retrying", method, path, e)
                await asyncio.sleep(backoff_delay(attempt))
                continue
            if r.status_code in RETRYABLE and method == "GET" and attempt < self.max_retries:
                log.warning("kalshi %s %s -> %s; backing off", method, path, r.status_code)
                await asyncio.sleep(backoff_delay(attempt))
                continue
            if r.status_code >= 400:
                raise ExchangeError(f"kalshi {method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise ExchangeError(f"kalshi {method} {path}: retries exhausted")

    async def _paginate(self, path: str, key: str, params: dict) -> list[dict]:
        out, cursor = [], None
        while True:
            p = dict(params, limit=params.get("limit", 200))
            if cursor:
                p["cursor"] = cursor
            data = await self._request("GET", path, params=p)
            out.extend(data.get(key) or [])
            cursor = data.get("cursor")
            if not cursor:
                return out

    # ---- public ----
    async def get_series(self, series_ticker: str) -> dict:
        return (await self._request("GET", f"/series/{series_ticker}"))["series"]

    async def list_market_dicts(self, *, series_ticker: str | None = None, status: str = "open") -> list[dict]:
        params: dict[str, Any] = {"status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        return await self._paginate("/markets", "markets", params)

    async def list_markets(self, *, series_ticker: str | None = None, status: str = "open",
                           series: dict | None = None) -> list[Market]:
        rows = await self.list_market_dicts(series_ticker=series_ticker, status=status)
        return [self.to_market(m, series) for m in rows]

    async def get_market(self, market_id: str) -> Market:
        return self.to_market((await self._request("GET", f"/markets/{market_id}"))["market"])

    async def get_orderbook(self, market_id: str) -> OrderBook:
        data = await self._request("GET", f"/markets/{market_id}/orderbook")
        return parse_orderbook(market_id, data, datetime.now(timezone.utc))

    async def get_orderbooks(self, market_ids: list[str]) -> dict[str, OrderBook]:
        out: dict[str, OrderBook] = {}
        for i in range(0, len(market_ids), MAX_BATCH_ORDERBOOKS):
            chunk = market_ids[i : i + MAX_BATCH_ORDERBOOKS]
            data = await self._request("GET", "/markets/orderbooks", params=[("tickers", t) for t in chunk])
            now = datetime.now(timezone.utc)
            for row in data.get("orderbooks") or []:
                out[row["ticker"]] = parse_orderbook(row["ticker"], row, now)
        missing = [t for t in market_ids if t not in out]
        if missing:
            log.warning("kalshi batch orderbook missing %d tickers; fetching individually", len(missing))
            for t in missing:
                out[t] = await self.get_orderbook(t)
        return out

    # ---- authenticated read (smoke test for credentials) ----
    async def get_balance(self) -> dict:
        return await self._request("GET", "/portfolio/balance", auth=True)

    @staticmethod
    def to_market(m: dict, series: dict | None = None) -> Market:
        rules = (m.get("rules_primary") or "") + "\n\n" + (m.get("rules_secondary") or "")
        sources = ", ".join(s.get("name", "") for s in (series or {}).get("settlement_sources", []))
        return Market(
            venue=Venue.KALSHI,
            market_id=m["ticker"],
            event_id=m.get("event_ticker", ""),
            series_id=(series or {}).get("ticker") or m.get("event_ticker", "").split("-")[0] or None,
            title=f"{m.get('title', '')} [{m.get('yes_sub_title', '')}]",
            rules=rules.strip(),
            settlement_source=sources,
            status=m.get("status", ""),
            close_time=_ts(m.get("close_time")),
            bracket=parse_bracket(m),
            raw=m,
        )
