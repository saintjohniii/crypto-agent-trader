from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class Position:
    symbol: str
    side: str  # LONG
    quantity: float
    entry_price: float
    leverage: float
    stop_loss: float
    take_profit: float
    opened_at: str


@dataclass
class Portfolio:
    cash_zar: float
    starting_zar: float
    positions: dict[str, Position] = field(default_factory=dict)
    daily_pnl_zar: float = 0.0
    daily_reset_date: str = ""
    halted: bool = False
    trade_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cash_zar": self.cash_zar,
            "starting_zar": self.starting_zar,
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "daily_pnl_zar": self.daily_pnl_zar,
            "daily_reset_date": self.daily_reset_date,
            "halted": self.halted,
            "trade_count": self.trade_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Portfolio:
        # Migrate legacy USDT paper state if present
        if "cash_zar" not in data and "cash_usdt" in data:
            data = {
                "cash_zar": float(data.get("starting_usdt", 0)) * 18.0,
                "starting_zar": float(data.get("starting_usdt", 0)) * 18.0,
                "positions": {},
                "daily_pnl_zar": 0.0,
                "daily_reset_date": data.get("daily_reset_date", ""),
                "halted": data.get("halted", False),
                "trade_count": 0,
            }
        positions = {
            sym: Position(**pos) for sym, pos in data.get("positions", {}).items()
        }
        return cls(
            cash_zar=data["cash_zar"],
            starting_zar=data["starting_zar"],
            positions=positions,
            daily_pnl_zar=data.get("daily_pnl_zar", 0.0),
            daily_reset_date=data.get("daily_reset_date", ""),
            halted=data.get("halted", False),
            trade_count=data.get("trade_count", 0),
        )


def load_portfolio(path: Path, starting_zar: float) -> Portfolio:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return Portfolio.from_dict(json.load(f))
    return Portfolio(cash_zar=starting_zar, starting_zar=starting_zar)


def save_portfolio(path: Path, portfolio: Portfolio) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(portfolio.to_dict(), f, indent=2)


def log_trade(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record["ts"] = datetime.now(timezone.utc).isoformat()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
