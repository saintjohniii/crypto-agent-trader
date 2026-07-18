from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.execution.portfolio import Portfolio, Position, log_trade
from src.strategy.technical import Signal


@dataclass
class Fill:
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    pnl: float = 0.0
    spread_cost: float = 0.0
    slippage_cost: float = 0.0


class PaperBroker:
    def __init__(self, fee_pct: float, trades_log, slippage_bps: float = 0):
        self.fee_pct = fee_pct
        self.trades_log = trades_log
        self.slippage_pct = slippage_bps / 10_000

    def _buy_fill(self, reference_price: float, ask_price: float | None) -> float:
        market_price = ask_price or reference_price
        return market_price * (1 + self.slippage_pct)

    def _sell_fill(self, reference_price: float, bid_price: float | None) -> float:
        market_price = bid_price or reference_price
        return market_price * (1 - self.slippage_pct)

    def estimate_close(
        self,
        pos: Position,
        reference_price: float,
        bid_price: float | None = None,
    ) -> tuple[float, float, float, float, float]:
        market_price = bid_price or reference_price
        fill_price = self._sell_fill(reference_price, bid_price)
        notional = pos.quantity * fill_price
        fee = notional * self.fee_pct
        spread_cost = pos.quantity * max(reference_price - market_price, 0)
        slippage_cost = pos.quantity * max(market_price - fill_price, 0)
        pnl = (fill_price - pos.entry_price) * pos.quantity * pos.leverage - fee
        return fill_price, fee, pnl, spread_cost, slippage_cost

    def check_stops(
        self,
        portfolio: Portfolio,
        prices: dict[str, float],
        bids: dict[str, float] | None = None,
    ) -> list[Fill]:
        fills: list[Fill] = []
        for sym in list(portfolio.positions.keys()):
            pos = portfolio.positions[sym]
            price = prices.get(sym)
            if price is None:
                continue
            if price <= pos.stop_loss or price >= pos.take_profit:
                reason = "stop_loss" if price <= pos.stop_loss else "take_profit"
                fill = self.close_long(
                    portfolio,
                    sym,
                    price,
                    reason=reason,
                    bid_price=(bids or {}).get(sym),
                )
                if fill:
                    fills.append(fill)
        return fills

    def open_long(
        self,
        portfolio: Portfolio,
        symbol: str,
        price: float,
        notional_zar: float,
        leverage: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        ask_price: float | None = None,
    ) -> Fill | None:
        if symbol in portfolio.positions:
            return None
        market_price = ask_price or price
        fill_price = self._buy_fill(price, ask_price)
        fee = notional_zar * self.fee_pct
        margin = notional_zar / leverage
        if margin + fee > portfolio.cash_zar:
            return None

        quantity = notional_zar / fill_price
        spread_cost = quantity * max(market_price - price, 0)
        slippage_cost = quantity * max(fill_price - market_price, 0)
        portfolio.cash_zar -= margin + fee
        portfolio.positions[symbol] = Position(
            symbol=symbol,
            side="LONG",
            quantity=quantity,
            entry_price=fill_price,
            leverage=leverage,
            stop_loss=fill_price * (1 - stop_loss_pct),
            take_profit=fill_price * (1 + take_profit_pct),
            opened_at=datetime.now(timezone.utc).isoformat(),
        )
        portfolio.trade_count += 1
        portfolio.daily_pnl_zar -= fee
        fill = Fill(
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            price=fill_price,
            fee=fee,
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
        )
        log_trade(
            self.trades_log,
            {
                "action": "OPEN_LONG",
                "symbol": symbol,
                "price": fill_price,
                "reference_price": price,
                "market_price": market_price,
                "quantity": quantity,
                "leverage": leverage,
                "fee": fee,
                "spread_cost": spread_cost,
                "slippage_cost": slippage_cost,
                "total_cost": fee + spread_cost + slippage_cost,
                "notional_zar": notional_zar,
            },
        )
        return fill

    def close_long(
        self,
        portfolio: Portfolio,
        symbol: str,
        price: float,
        reason: str = "signal",
        bid_price: float | None = None,
    ) -> Fill | None:
        pos = portfolio.positions.pop(symbol, None)
        if not pos:
            return None

        fill_price, fee, pnl, spread_cost, slippage_cost = self.estimate_close(
            pos, price, bid_price
        )
        margin = (pos.quantity * pos.entry_price) / pos.leverage
        portfolio.cash_zar += margin + pnl
        portfolio.daily_pnl_zar += pnl
        portfolio.trade_count += 1

        fill = Fill(
            symbol=symbol,
            side="SELL",
            quantity=pos.quantity,
            price=fill_price,
            fee=fee,
            pnl=pnl,
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
        )
        log_trade(
            self.trades_log,
            {
                "action": "CLOSE_LONG",
                "symbol": symbol,
                "price": fill_price,
                "reference_price": price,
                "market_price": bid_price or price,
                "quantity": pos.quantity,
                "pnl": pnl,
                "fee": fee,
                "spread_cost": spread_cost,
                "slippage_cost": slippage_cost,
                "total_cost": fee + spread_cost + slippage_cost,
                "reason": reason,
            },
        )
        return fill

    def execute_signal(
        self,
        portfolio: Portfolio,
        symbol: str,
        signal: Signal,
        price: float,
        notional_zar: float,
        leverage: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        market_price: float | None = None,
    ) -> Fill | None:
        if signal == Signal.BUY:
            return self.open_long(
                portfolio,
                symbol,
                price,
                notional_zar,
                leverage,
                stop_loss_pct,
                take_profit_pct,
                ask_price=market_price,
            )
        if signal == Signal.SELL and symbol in portfolio.positions:
            return self.close_long(
                portfolio, symbol, price, reason="signal", bid_price=market_price
            )
        return None
