from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.data.market import Candle
from src.strategy.technical import ema


class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"


@dataclass
class RegimeSignal:
    regime: Regime
    atr: float
    atr_mean: float
    ema_gap_pct: float
    reason: str


def atr(candles: list[Candle], period: int = 14) -> list[float]:
    if len(candles) < 2:
        return [0.0] * len(candles)
    trs: list[float] = [candles[0].high - candles[0].low]
    for i in range(1, len(candles)):
        c = candles[i]
        prev = candles[i - 1]
        tr = max(
            c.high - c.low,
            abs(c.high - prev.close),
            abs(c.low - prev.close),
        )
        trs.append(tr)
    out: list[float] = []
    for i in range(len(trs)):
        window = trs[max(0, i - period + 1) : i + 1]
        out.append(sum(window) / len(window))
    return out


def classify_regime(
    candles: list[Candle],
    ema_fast: int,
    ema_slow: int,
    atr_period: int = 14,
    range_atr_mult: float = 0.8,
    trend_ema_pct: float = 0.002,
) -> RegimeSignal:
    if len(candles) < max(ema_slow, atr_period) + 2:
        return RegimeSignal(
            Regime.RANGE, 0.0, 0.0, 0.0, "insufficient candles for regime"
        )

    closes = [c.close for c in candles]
    price = closes[-1]
    fast = ema(closes, ema_fast)
    slow = ema(closes, ema_slow)
    atrs = atr(candles, atr_period)
    cur_atr = atrs[-1]
    lookback = min(50, len(atrs))
    atr_mean = sum(atrs[-lookback:]) / lookback if lookback else cur_atr
    gap_pct = (fast[-1] - slow[-1]) / price if price else 0.0

    low_vol = atr_mean > 0 and cur_atr < atr_mean * range_atr_mult
    if low_vol or abs(gap_pct) < trend_ema_pct:
        return RegimeSignal(
            Regime.RANGE,
            cur_atr,
            atr_mean,
            gap_pct,
            f"range/chop ATR={cur_atr:.4f} gap={gap_pct*100:.2f}%",
        )

    if gap_pct >= trend_ema_pct:
        return RegimeSignal(
            Regime.TREND_UP,
            cur_atr,
            atr_mean,
            gap_pct,
            f"trend up gap={gap_pct*100:.2f}%",
        )

    return RegimeSignal(
        Regime.TREND_DOWN,
        cur_atr,
        atr_mean,
        gap_pct,
        f"trend down gap={gap_pct*100:.2f}%",
    )


def allows_new_long(regime: Regime, enabled: bool = True) -> bool:
    if not enabled:
        return True
    return regime == Regime.TREND_UP
