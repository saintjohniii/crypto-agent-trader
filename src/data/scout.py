"""24/7 news scout — polls feeds, dedupes, scores per coin, caches for the agent."""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests

from src.config import ROOT, load_config
from src.data.news import BEARISH, BULLISH, score_headline

UA = {"User-Agent": "CryptoAgentTrader/1.0"}

# Per-coin keyword mapping (Luno symbols -> topic words)
COIN_KEYWORDS: dict[str, list[str]] = {
    "XBT": ["bitcoin", "btc", "xbt"],
    "ETH": ["ethereum", "eth ", "ether "],
    "XRP": ["xrp", "ripple"],
    "SOL": ["solana", "sol "],
}

# Rough source reliability weights (default 1.0)
SOURCE_WEIGHTS = {
    "coindesk.com": 1.2,
    "cointelegraph.com": 1.0,
    "decrypt.co": 1.0,
    "cryptoslate.com": 0.9,
    "bitcoinmagazine.com": 0.9,
    "bbci.co.uk": 1.1,
    "news.google.com": 0.8,
}


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower())[:80]


def _source_weight(url: str) -> float:
    for host, w in SOURCE_WEIGHTS.items():
        if host in url:
            return w
    return 1.0


def _entry_age_hours(entry: Any) -> float:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                ts = time.mktime(parsed)
                return max(0.0, (time.time() - ts) / 3600)
            except (TypeError, ValueError, OverflowError):
                continue
    return 6.0  # unknown age: assume half-life


def scout_once(cfg: dict | None = None, verbose: bool = False) -> dict[str, Any]:
    cfg = cfg or load_config()
    news_cfg = cfg["news"]
    scout_cfg = news_cfg.get("scout") or {}
    half_life = float(scout_cfg.get("half_life_hours", 6))
    max_per_feed = int(scout_cfg.get("max_per_feed", 20))

    seen: set[str] = set()
    items: list[dict[str, Any]] = []

    for url in news_cfg["feeds"]:
        try:
            resp = requests.get(url, timeout=12, headers=UA)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            if verbose:
                print(f"  feed error {url}: {exc}")
            continue

        weight = _source_weight(url)
        for entry in parsed.entries[:max_per_feed]:
            title = _clean(entry.get("title", ""))
            if not title:
                continue
            key = _norm_title(title)
            if key in seen:
                continue
            seen.add(key)
            summary = _clean(entry.get("summary", ""))
            blob = f"{title} {summary}".lower()
            raw = score_headline(blob)
            age_h = _entry_age_hours(entry)
            recency = math.pow(0.5, age_h / half_life)
            coins = [
                coin
                for coin, words in COIN_KEYWORDS.items()
                if any(w in blob for w in words)
            ]
            items.append(
                {
                    "title": title[:140],
                    "score": raw,
                    "weight": weight * recency,
                    "age_hours": round(age_h, 1),
                    "coins": coins,
                    "source": url,
                }
            )

    def _weighted(rows: list[dict[str, Any]]) -> float:
        scored = [r for r in rows if r["score"] != 0]
        pool = scored or rows
        total_w = sum(r["weight"] for r in pool)
        if total_w <= 0:
            return 0.0
        val = sum(r["score"] * r["weight"] for r in pool) / total_w
        return max(-1.0, min(1.0, val))

    global_score = _weighted(items)
    per_coin: dict[str, dict[str, Any]] = {}
    for coin in COIN_KEYWORDS:
        rows = [r for r in items if coin in r["coins"]]
        # Only non-zero rows drive the score, so that count is what callers must
        # judge its reliability by — a coin can match 12 headlines but score off 2
        scoring = [r for r in rows if r["score"] != 0]
        per_coin[coin] = {
            "score": round(_weighted(rows), 3) if rows else None,
            "headlines": len(rows),
            "scoring_headlines": len(scoring),
        }

    movers = sorted(items, key=lambda r: (abs(r["score"]) * r["weight"]), reverse=True)
    top = [r["title"] for r in movers[:8]] or [r["title"] for r in items[:8]]

    cache = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "score": round(global_score, 3),
        "headline_count": len(items),
        "feeds_ok": len({r["source"] for r in items}),
        "feeds_total": len(news_cfg["feeds"]),
        "per_coin": per_coin,
        "top_headlines": top,
    }

    cache_path = ROOT / cfg["paths"].get("news_cache", "data/news_cache.json")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    log_path = ROOT / cfg["paths"].get("news_log", "data/news_log.jsonl")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": cache["updated_at"],
                    "score": cache["score"],
                    "headline_count": cache["headline_count"],
                    "per_coin": {k: v["score"] for k, v in per_coin.items()},
                }
            )
            + "\n"
        )

    return cache


def load_cache(max_age_min: float | None = None) -> dict[str, Any] | None:
    cfg = load_config()
    cache_path = ROOT / cfg["paths"].get("news_cache", "data/news_cache.json")
    if not cache_path.exists():
        return None
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if max_age_min is not None:
        try:
            updated = datetime.fromisoformat(cache["updated_at"])
            age_min = (datetime.now(timezone.utc) - updated).total_seconds() / 60
            cache["age_min"] = round(age_min, 1)
            if age_min > max_age_min:
                return None
        except (KeyError, ValueError):
            return None
    return cache


def run_scout_loop() -> None:
    cfg = load_config()
    interval = int((cfg["news"].get("scout") or {}).get("interval_seconds", 300))
    print(f"News scout — 24/7 | refresh every {interval}s | Ctrl+C to stop")
    print(f"Feeds: {len(cfg['news']['feeds'])}")
    print()
    while True:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        try:
            cache = scout_once(cfg, verbose=True)
            coins = " ".join(
                f"{k}={v['score']}" for k, v in cache["per_coin"].items() if v["score"] is not None
            )
            print(
                f"[{ts}] scouted {cache['headline_count']} headlines "
                f"({cache['feeds_ok']}/{cache['feeds_total']} feeds) | "
                f"global {cache['score']:+.2f} | {coins}"
            )
        except Exception as exc:
            print(f"[{ts}] ERROR: {exc}")
        time.sleep(interval)
