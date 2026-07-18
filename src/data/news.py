from __future__ import annotations

import re
from dataclasses import dataclass

import feedparser
import requests

BULLISH = {
    "surge", "rally", "inflow", "approval", "adoption", "breakout", "record",
    "bullish", "gain", "soar", "etf inflow", "rate cut", "easing",
}
BEARISH = {
    "crash", "hack", "ban", "selloff", "outflow", "lawsuit", "sec sue",
    "bearish", "plunge", "collapse", "war", "strike", "sanction", "rate hike",
    "liquidation", "fraud", "bankrupt",
}


@dataclass
class NewsSentiment:
    score: float  # -1 to +1
    headline_count: int
    top_headlines: list[str]
    per_coin: dict | None = None  # e.g. {"XBT": {"score": 0.2, "headlines": 4}}
    source: str = "live"  # "scout" when served from the 24/7 cache
    age_min: float | None = None

    def score_for(self, symbol: str, blend: float = 0.5) -> float:
        """Blend global score with coin-specific score when the scout has one."""
        if not self.per_coin:
            return self.score
        coin = symbol[:3]
        entry = self.per_coin.get(coin)
        if not entry or entry.get("score") is None:
            return self.score
        return max(-1.0, min(1.0, (1 - blend) * self.score + blend * entry["score"]))


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def score_headline(text: str) -> float:
    bull = sum(1 for w in BULLISH if w in text)
    bear = sum(1 for w in BEARISH if w in text)
    if bull == 0 and bear == 0:
        return 0.0
    return (bull - bear) / max(bull + bear, 1)


def fetch_news_sentiment(feed_urls: list[str], max_per_feed: int = 10) -> NewsSentiment:
    # Prefer the 24/7 scout cache when it's fresh — richer scoring, zero latency
    try:
        from src.config import load_config
        from src.data.scout import load_cache

        cfg = load_config()
        max_age = float(
            (cfg["news"].get("scout") or {}).get("cache_max_age_min", 15)
        )
        cache = load_cache(max_age_min=max_age)
        if cache and cache.get("headline_count", 0) >= 5:
            return NewsSentiment(
                score=float(cache["score"]),
                headline_count=int(cache["headline_count"]),
                top_headlines=list(cache.get("top_headlines", []))[:8],
                per_coin=cache.get("per_coin"),
                source="scout",
                age_min=cache.get("age_min"),
            )
    except Exception:
        pass  # scout unavailable — fall back to direct fetch

    headlines: list[str] = []
    scores: list[float] = []

    for url in feed_urls:
        try:
            resp = requests.get(url, timeout=12, headers={"User-Agent": "CryptoAgentTrader/1.0"})
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception:
            continue

        for entry in parsed.entries[:max_per_feed]:
            title = _clean(entry.get("title", ""))
            summary = _clean(entry.get("summary", ""))
            blob = f"{title} {summary}"
            if not blob.strip():
                continue
            headlines.append(entry.get("title", title)[:120])
            scores.append(score_headline(blob))

    if not scores:
        return NewsSentiment(score=0.0, headline_count=0, top_headlines=[])

    avg = sum(scores) / len(scores)
    return NewsSentiment(
        score=max(-1.0, min(1.0, avg)),
        headline_count=len(scores),
        top_headlines=headlines[:5],
    )
