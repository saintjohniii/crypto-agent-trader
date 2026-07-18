from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.execution.portfolio import Portfolio


@dataclass
class OrderPlan:
    approved: bool
    notional_zar: float
    leverage: float
    reason: str


class RiskManager:
    def __init__(self, risk_cfg: dict, leverage_cfg: dict, fees_cfg: dict | None = None, execution_cfg: dict | None = None):
        self.max_position_pct = risk_cfg["max_position_pct"]
        self.stop_loss_pct = risk_cfg["stop_loss_pct"]
        self.take_profit_pct = risk_cfg["take_profit_pct"]
        self.max_daily_loss_pct = risk_cfg["max_daily_loss_pct"]
        self.max_open_positions = risk_cfg["max_open_positions"]
        self.min_order_zar = risk_cfg.get("min_order_zar", risk_cfg.get("min_order_usdt", 50))
        self.min_reward_cost_multiple = float(risk_cfg.get("min_reward_cost_multiple", 3.0))
        self.leverage_enabled = leverage_cfg["enabled"]
        self.max_leverage = leverage_cfg["max"]
        self.default_leverage = leverage_cfg["default"]

        fees_cfg = fees_cfg or {}
        execution_cfg = execution_cfg or {}
        self.taker_pct = float(fees_cfg.get("taker_pct", 0.001))
        self.slippage_pct = float(execution_cfg.get("slippage_bps", 0)) / 10_000

        atr_cfg = risk_cfg.get("atr_stops") or {}
        self.atr_enabled = bool(atr_cfg.get("enabled", False))
        self.atr_stop_mult = float(atr_cfg.get("stop_mult", 2.0))
        self.atr_tp_mult = float(atr_cfg.get("tp_mult", 4.0))
        self.atr_min_stop = float(atr_cfg.get("min_stop_pct", 0.008))
        self.atr_max_stop = float(atr_cfg.get("max_stop_pct", 0.04))
        self.risk_per_trade_pct = float(risk_cfg.get("risk_per_trade_pct", 0) or 0)

    def stop_tp_pcts(self, atr: float, price: float) -> tuple[float, float]:
        """SL/TP fractions for this entry — ATR-scaled when enabled, else static config."""
        if not self.atr_enabled or atr <= 0 or price <= 0:
            return self.stop_loss_pct, self.take_profit_pct
        stop = min(self.atr_max_stop, max(self.atr_min_stop, self.atr_stop_mult * atr / price))
        tp = stop * (self.atr_tp_mult / self.atr_stop_mult)
        return stop, tp

    def reset_daily_if_needed(self, portfolio: Portfolio) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if portfolio.daily_reset_date != today:
            portfolio.daily_reset_date = today
            portfolio.daily_pnl_zar = 0.0
            portfolio.halted = False

    def check_halt(self, portfolio: Portfolio) -> bool:
        if portfolio.halted:
            return True
        max_loss = portfolio.starting_zar * self.max_daily_loss_pct
        if portfolio.daily_pnl_zar <= -max_loss:
            portfolio.halted = True
            return True
        return False

    def round_trip_cost_pct(self, spread_bps: float) -> float:
        half_spread = (spread_bps / 10_000) / 2
        per_side = self.taker_pct + self.slippage_pct + half_spread
        return 2 * per_side

    def plan_entry(
        self,
        portfolio: Portfolio,
        symbol: str,
        equity_zar: float,
        size_multiplier: float,
        spread_bps: float = 0.0,
        stop_pct: float | None = None,
        tp_pct: float | None = None,
    ) -> OrderPlan:
        stop_pct = stop_pct if stop_pct else self.stop_loss_pct
        tp_pct = tp_pct if tp_pct else self.take_profit_pct
        if self.check_halt(portfolio):
            return OrderPlan(False, 0, 1, "daily loss limit — trading halted")

        if len(portfolio.positions) >= self.max_open_positions:
            return OrderPlan(False, 0, 1, "max open positions reached")

        if symbol in portfolio.positions:
            return OrderPlan(False, 0, 1, "already in position")

        leverage = 1.0
        if self.leverage_enabled:
            leverage = min(self.max_leverage, max(1.0, self.default_leverage))

        cap_notional = equity_zar * self.max_position_pct
        if self.risk_per_trade_pct > 0 and stop_pct > 0:
            base_notional = min(cap_notional, equity_zar * self.risk_per_trade_pct / stop_pct)
        else:
            base_notional = cap_notional
        base_notional *= size_multiplier
        notional = max(self.min_order_zar, base_notional * leverage)

        margin_required = notional / leverage
        if margin_required > portfolio.cash_zar:
            notional = max(0, portfolio.cash_zar * leverage * 0.95)
            margin_required = notional / leverage

        if notional < self.min_order_zar:
            return OrderPlan(False, 0, leverage, "insufficient capital for min order")

        cost_pct = self.round_trip_cost_pct(spread_bps)
        expected_reward = tp_pct * notional
        round_trip_cost = cost_pct * notional
        if round_trip_cost > 0:
            multiple = expected_reward / round_trip_cost
            if multiple < self.min_reward_cost_multiple:
                return OrderPlan(
                    False,
                    0,
                    leverage,
                    (
                        f"reward/cost {multiple:.2f}x < "
                        f"{self.min_reward_cost_multiple:.1f}x "
                        f"(TP {tp_pct*100:.1f}% vs cost {cost_pct*100:.2f}%)"
                    ),
                )

        return OrderPlan(True, notional, leverage, "approved")
