from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

load_dotenv()

LUNO_BASE = "https://api.luno.com/api/1"
LUNO_EXCHANGE = "https://api.luno.com/api/exchange/1"
UA = {"User-Agent": "CryptoAgentTrader/1.0"}

INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
}


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _auth() -> tuple[str, str] | None:
    key = (os.getenv("LUNO_API_KEY_ID") or "").strip()
    secret = (os.getenv("LUNO_API_KEY_SECRET") or "").strip()
    if key and secret:
        return key, secret
    return None


def has_credentials() -> bool:
    return _auth() is not None


def fetch_ticker_price(pair: str) -> float:
    resp = requests.get(
        f"{LUNO_BASE}/ticker",
        params={"pair": pair},
        timeout=15,
        headers=UA,
    )
    resp.raise_for_status()
    return float(resp.json()["last_trade"])


def fetch_ticker(pair: str) -> dict:
    resp = requests.get(
        f"{LUNO_BASE}/ticker",
        params={"pair": pair},
        timeout=15,
        headers=UA,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "pair": data.get("pair", pair),
        "bid": float(data["bid"]),
        "ask": float(data["ask"]),
        "last": float(data["last_trade"]),
        "volume_24h": float(data.get("rolling_24_hour_volume", 0)),
    }


def fetch_balances() -> list[dict]:
    """Read-only account balances (requires API key)."""
    auth = _auth()
    if not auth:
        raise RuntimeError("Luno API credentials missing in .env")
    resp = requests.get(
        f"{LUNO_BASE}/balance",
        timeout=15,
        headers=UA,
        auth=auth,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(data.get("error") or data.get("error_code") or "balance error")
    return data.get("balance", [])


def _price_for_asset(asset: str) -> tuple[float | None, str | None]:
    """Return (price_in_zar_or_native, note). ZAR assets priced 1:1."""
    if asset == "ZAR":
        return 1.0, "ZAR"
    if asset == "XBT":
        return fetch_ticker_price("XBTZAR"), "XBTZAR"
    candidates = [f"{asset}ZAR"]
    # Luno uses XBT not BTC
    if asset == "BTC":
        candidates = ["XBTZAR"]
    for pair in candidates:
        try:
            return fetch_ticker_price(pair), pair
        except Exception:
            continue
    # Non-ZAR quote (e.g. SEIMYR) — return quote price with note, no ZAR conversion
    try:
        resp = requests.get(
            f"{LUNO_BASE}/tickers",
            timeout=15,
            headers=UA,
        )
        resp.raise_for_status()
        for t in resp.json().get("tickers", []):
            pair = t.get("pair", "")
            if pair.startswith(asset):
                return float(t["last_trade"]), pair
    except Exception:
        pass
    return None, None


def fetch_wallet_summary() -> dict:
    """Live Luno wallet holdings for the dashboard."""
    if not has_credentials():
        return {"connected": False, "holdings": [], "total_zar": None, "error": "no credentials"}

    try:
        balances = fetch_balances()
    except Exception as exc:
        return {"connected": False, "holdings": [], "total_zar": None, "error": str(exc)[:160]}

    holdings = []
    total_zar = 0.0
    zar_complete = True

    for b in balances:
        asset = b.get("asset") or "?"
        bal = float(b.get("balance") or 0)
        reserved = float(b.get("reserved") or 0)
        unconfirmed = float(b.get("unconfirmed") or 0)
        total = bal + reserved + unconfirmed
        if total <= 0:
            continue

        price, pair = _price_for_asset(asset)
        value_zar = None
        value_note = None
        if asset == "ZAR":
            value_zar = total
        elif pair and pair.endswith("ZAR") and price is not None:
            value_zar = total * price
        elif pair and price is not None:
            zar_complete = False
            value_note = f"{price} {pair[len(asset):]}"
        else:
            zar_complete = False

        if value_zar is not None:
            total_zar += value_zar

        holdings.append(
            {
                "asset": asset,
                "balance": bal,
                "reserved": reserved,
                "unconfirmed": unconfirmed,
                "total": total,
                "pair": pair,
                "value_zar": round(value_zar, 2) if value_zar is not None else None,
                "value_note": value_note,
            }
        )

    holdings.sort(key=lambda h: (h["value_zar"] is None, -(h["value_zar"] or 0), h["asset"]))

    return {
        "connected": True,
        "holdings": holdings,
        "total_zar": round(total_zar, 2) if holdings else 0.0,
        "zar_complete": zar_complete,
        "error": None,
    }

def _fetch_trades_page(pair: str, since_ms: int | None = None) -> list[dict]:
    params: dict = {"pair": pair}
    if since_ms is not None:
        params["since"] = since_ms
    resp = requests.get(
        f"{LUNO_BASE}/trades",
        params=params,
        timeout=20,
        headers=UA,
    )
    resp.raise_for_status()
    return resp.json().get("trades", [])


def fetch_recent_trades(pair: str, max_pages: int = 8) -> list[dict]:
    now_ms = int(time.time() * 1000)
    window_start = now_ms - (24 * 60 * 60 * 1000) + 5_000
    collected: dict[int, dict] = {}
    since = window_start

    for _ in range(max_pages):
        batch = _fetch_trades_page(pair, since_ms=since)
        if not batch:
            break
        for trade in batch:
            seq = int(trade.get("sequence", 0))
            collected[seq] = trade
        oldest_ts = min(int(t["timestamp"]) for t in batch)
        newest_ts = max(int(t["timestamp"]) for t in batch)
        if len(batch) < 100:
            break
        if oldest_ts <= since:
            since = newest_ts
        else:
            since = oldest_ts
        if since >= now_ms:
            break

    trades = list(collected.values())
    trades.sort(key=lambda t: int(t["timestamp"]))
    return trades


def trades_to_candles(trades: list[dict], duration_sec: int) -> list[Candle]:
    if not trades:
        return []

    buckets: dict[int, list[dict]] = defaultdict(list)
    for trade in trades:
        ts = int(trade["timestamp"])
        bucket = (ts // (duration_sec * 1000)) * (duration_sec * 1000)
        buckets[bucket].append(trade)

    candles: list[Candle] = []
    for open_time in sorted(buckets.keys()):
        rows = buckets[open_time]
        prices = [float(t["price"]) for t in rows]
        volume = sum(float(t["volume"]) for t in rows)
        candles.append(
            Candle(
                open_time=open_time,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=volume,
            )
        )
    return candles


def _rows_to_candles(rows: list[dict]) -> list[Candle]:
    candles: list[Candle] = []
    for row in rows:
        candles.append(
            Candle(
                open_time=int(row["timestamp"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0)),
            )
        )
    return candles


def fetch_auth_candles(pair: str, duration_sec: int, limit: int = 100) -> list[Candle]:
    auth = _auth()
    if not auth:
        return []

    since = int(time.time() * 1000) - (duration_sec * 1000 * (limit + 5))
    resp = requests.get(
        f"{LUNO_EXCHANGE}/candles",
        params={"pair": pair, "since": since, "duration": duration_sec},
        timeout=20,
        headers=UA,
        auth=auth,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(data.get("error") or "candles error")

    return _rows_to_candles(data.get("candles", []))[-limit:]


def fetch_historical_candles(
    pair: str, interval: str = "15m", bars: int = 2000
) -> list[Candle]:
    """Paginate authenticated candles (or fall back) for backtests."""
    duration = INTERVAL_SECONDS.get(interval, 900)
    if has_credentials():
        auth = _auth()
        assert auth is not None
        now_ms = int(time.time() * 1000)
        since = now_ms - (duration * 1000 * (bars + 20))
        by_ts: dict[int, Candle] = {}
        cursor = since
        for _ in range(40):
            resp = requests.get(
                f"{LUNO_EXCHANGE}/candles",
                params={"pair": pair, "since": cursor, "duration": duration},
                timeout=25,
                headers=UA,
                auth=auth,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                raise RuntimeError(data.get("error") or "candles error")
            batch = _rows_to_candles(data.get("candles", []))
            if not batch:
                break
            for c in batch:
                by_ts[c.open_time] = c
            last_ts = batch[-1].open_time
            if last_ts <= cursor:
                break
            cursor = last_ts + duration * 1000
            if len(by_ts) >= bars and last_ts >= now_ms - duration * 1000:
                break
            if cursor >= now_ms:
                break
        candles = [by_ts[k] for k in sorted(by_ts)]
        if len(candles) >= 50:
            return candles[-bars:]

    return fetch_klines(pair, interval, limit=min(bars, 500))


def fetch_klines(pair: str, interval: str = "1h", limit: int = 100) -> list[Candle]:
    duration = INTERVAL_SECONDS.get(interval, 3600)

    if has_credentials():
        try:
            auth_candles = fetch_auth_candles(pair, duration, limit=limit)
            if len(auth_candles) >= 20:
                return auth_candles
        except Exception:
            pass  # fall back to public trades

    trades = fetch_recent_trades(pair)
    candles = trades_to_candles(trades, duration)
    if len(candles) < 30 and interval == "1h":
        candles = trades_to_candles(trades, INTERVAL_SECONDS["15m"])
    if len(candles) < 30:
        candles = trades_to_candles(trades, INTERVAL_SECONDS["5m"])

    if not candles:
        price = fetch_ticker_price(pair)
        now = int(time.time() * 1000)
        candles = [
            Candle(
                open_time=now - i * duration * 1000,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=0.0,
            )
            for i in range(limit, 0, -1)
        ]

    return candles[-limit:]
