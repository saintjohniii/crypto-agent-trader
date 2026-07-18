from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.data.market import Candle


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class TechnicalSignal:
    action: Signal
    reason: str
    rsi: float
    ema_fast: float
    ema_slow: float
    price: float


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def analyze(
    candles: list[Candle],
    ema_fast: int,
    ema_slow: int,
    rsi_period: int,
    rsi_overbought: float,
    rsi_oversold: float,
    breakout_period: int,
) -> TechnicalSignal:
    closes = [c.close for c in candles]
    price = closes[-1]
    fast = ema(closes, ema_fast)
    slow = ema(closes, ema_slow)
    rsi_val = rsi(closes, rsi_period)

    high_20 = max(c.high for c in candles[-breakout_period:])
    low_20 = min(c.low for c in candles[-breakout_period:])

    bullish_cross = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
    bearish_cross = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
    breakout_up = price >= high_20 * 0.999
    breakdown = price <= low_20 * 1.001

    if bearish_cross or (rsi_val > rsi_overbought and breakdown):
        return TechnicalSignal(
            action=Signal.SELL,
            reason=f"bearish EMA cross or RSI={rsi_val:.1f} breakdown",
            rsi=rsi_val,
            ema_fast=fast[-1],
            ema_slow=slow[-1],
            price=price,
        )

    if (bullish_cross or breakout_up) and rsi_val < rsi_overbought:
        return TechnicalSignal(
            action=Signal.BUY,
            reason=f"bullish EMA cross or breakout, RSI={rsi_val:.1f}",
            rsi=rsi_val,
            ema_fast=fast[-1],
            ema_slow=slow[-1],
            price=price,
        )

    if rsi_val < rsi_oversold and fast[-1] > slow[-1]:
        return TechnicalSignal(
            action=Signal.BUY,
            reason=f"oversold bounce RSI={rsi_val:.1f}",
            rsi=rsi_val,
            ema_fast=fast[-1],
            ema_slow=slow[-1],
            price=price,
        )

    return TechnicalSignal(
        action=Signal.HOLD,
        reason="no clear setup",
        rsi=rsi_val,
        ema_fast=fast[-1],
        ema_slow=slow[-1],
        price=price,
    )
