from __future__ import annotations

from dataclasses import dataclass

from src.data.news import NewsSentiment
from src.strategy.technical import Signal, TechnicalSignal


@dataclass
class HybridDecision:
    action: Signal
    reason: str
    technical: TechnicalSignal
    news_score: float
    size_multiplier: float  # 0-1.5


def combine(
    technical: TechnicalSignal,
    news: NewsSentiment,
    block_threshold: float,
    boost_threshold: float,
) -> HybridDecision:
    news_score = news.score

    if technical.action == Signal.BUY:
        if news_score <= block_threshold:
            return HybridDecision(
                action=Signal.HOLD,
                reason=f"news filter blocked buy (sentiment={news_score:.2f})",
                technical=technical,
                news_score=news_score,
                size_multiplier=0.0,
            )
        multiplier = 1.0
        if news_score >= boost_threshold:
            multiplier = 1.25
        return HybridDecision(
            action=Signal.BUY,
            reason=f"{technical.reason} + news OK ({news_score:.2f})",
            technical=technical,
            news_score=news_score,
            size_multiplier=multiplier,
        )

    if technical.action == Signal.SELL:
        return HybridDecision(
            action=Signal.SELL,
            reason=technical.reason,
            technical=technical,
            news_score=news_score,
            size_multiplier=1.0,
        )

    # HOLD but news very negative with long bias — optional exit nudge handled in runner via positions
    return HybridDecision(
        action=Signal.HOLD,
        reason=technical.reason,
        technical=technical,
        news_score=news_score,
        size_multiplier=0.0,
    )
