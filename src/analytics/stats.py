from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def log_equity(path: Path, equity_zar: float, extra: dict[str, Any] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "equity_zar": round(equity_zar, 2),
    }
    if extra:
        record.update(extra)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def max_drawdown_pct(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _pair_round_trips(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    open_stack: dict[str, list[dict[str, Any]]] = defaultdict(list)
    closed: list[dict[str, Any]] = []
    for t in trades:
        action = t.get("action")
        sym = t.get("symbol")
        if not sym:
            continue
        if action == "OPEN_LONG":
            open_stack[sym].append(t)
        elif action == "CLOSE_LONG":
            entry = open_stack[sym].pop(0) if open_stack[sym] else None
            closed.append(
                {
                    "symbol": sym,
                    "entry_ts": entry.get("ts") if entry else None,
                    "exit_ts": t.get("ts"),
                    "entry_price": entry.get("price") if entry else None,
                    "exit_price": t.get("price"),
                    "pnl": float(t.get("pnl") or 0),
                    "fee": float(t.get("fee") or 0),
                    "total_cost": float(t.get("total_cost") or t.get("fee") or 0),
                    "reason": t.get("reason"),
                }
            )
    return closed


def compute_stats(
    trades_path: Path,
    equity_path: Path | None = None,
    starting_zar: float | None = None,
) -> dict[str, Any]:
    trades = _read_jsonl(trades_path)
    closed = _pair_round_trips(trades)

    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    net_pnl = sum(t["pnl"] for t in closed)
    win_rate = (len(wins) / len(closed) * 100) if closed else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0

    by_symbol: dict[str, dict[str, Any]] = {}
    for t in closed:
        sym = t["symbol"]
        bucket = by_symbol.setdefault(
            sym,
            {
                "symbol": sym,
                "trades": 0,
                "wins": 0,
                "net_pnl": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
            },
        )
        bucket["trades"] += 1
        bucket["net_pnl"] += t["pnl"]
        if t["pnl"] > 0:
            bucket["wins"] += 1
            bucket["gross_profit"] += t["pnl"]
        else:
            bucket["gross_loss"] += abs(t["pnl"])

    per_pair = []
    for sym, b in sorted(by_symbol.items()):
        gl = b["gross_loss"]
        pf = (b["gross_profit"] / gl) if gl > 0 else (float("inf") if b["gross_profit"] > 0 else 0.0)
        per_pair.append(
            {
                "symbol": sym,
                "trades": b["trades"],
                "wins": b["wins"],
                "win_rate": round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0.0,
                "net_pnl": round(b["net_pnl"], 2),
                "profit_factor": round(pf, 2) if pf != float("inf") else None,
                "profit_factor_display": "∞" if pf == float("inf") else round(pf, 2),
            }
        )

    equity_rows = _read_jsonl(equity_path) if equity_path else []
    curve = [float(r["equity_zar"]) for r in equity_rows if "equity_zar" in r]
    if not curve and starting_zar is not None:
        curve = [starting_zar]
        if closed:
            eq = starting_zar
            for t in closed:
                eq += t["pnl"]
                curve.append(eq)
    max_dd = max_drawdown_pct(curve)

    return {
        "closed_trades": len(closed),
        "open_fills": sum(1 for t in trades if t.get("action") == "OPEN_LONG"),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "profit_factor_display": "∞" if profit_factor == float("inf") else round(profit_factor, 2),
        "net_pnl": round(net_pnl, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "equity_points": len(curve),
        "per_pair": per_pair,
    }


def load_backtest_report(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
