from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.analytics.stats import max_drawdown_pct
from src.config import ROOT, load_config
from src.data.luno import INTERVAL_SECONDS, Candle, fetch_historical_candles
from src.data.news import NewsSentiment
from src.execution.paper_broker import PaperBroker
from src.execution.portfolio import Portfolio
from src.risk.manager import RiskManager
from src.strategy.hybrid import combine
from src.strategy.regime import Regime, allows_new_long, classify_regime, htf_trend_up
from src.strategy.technical import Signal, analyze


def _synthetic_book(price: float, assumed_spread_bps: float) -> tuple[float, float]:
    half = price * (assumed_spread_bps / 10_000) / 2
    return price - half, price + half


def _equity(portfolio: Portfolio, marks: dict[str, float]) -> float:
    eq = portfolio.cash_zar
    for sym, pos in portfolio.positions.items():
        mark = marks.get(sym, pos.entry_price)
        margin = (pos.quantity * pos.entry_price) / pos.leverage
        unrealized = (mark - pos.entry_price) * pos.quantity * pos.leverage
        eq += margin + unrealized
    return eq


def run_backtest(days: int | None = None, bars: int | None = None) -> dict[str, Any]:
    cfg = load_config()
    interval = cfg["trading"]["interval"]
    duration = INTERVAL_SECONDS.get(interval, 900)
    if bars is None:
        day_count = days if days is not None else 30
        bars = max(200, int(day_count * 24 * 3600 / duration))

    symbols = list(cfg["trading"]["symbols"])
    starting = float(cfg["capital"]["starting_zar"])
    assumed_spread = float(cfg.get("execution", {}).get("assumed_spread_bps", 10))
    strat = cfg["strategy"]
    regime_cfg = strat.get("regime") or {}
    regime_enabled = bool(regime_cfg.get("enabled", True))
    htf_cfg = strat.get("htf_filter") or {}
    htf_enabled = bool(htf_cfg.get("enabled", False))
    htf_seconds = INTERVAL_SECONDS.get(htf_cfg.get("interval", "4h"), 14400)
    range_cfg = strat.get("range_mode") or {}
    range_enabled = bool(range_cfg.get("enabled", False))
    warmup = max(strat["ema_slow"], strat["breakout_period"], regime_cfg.get("atr_period", 14)) + 5

    series: dict[str, list[Candle]] = {}
    for sym in symbols:
        series[sym] = fetch_historical_candles(sym, interval, bars=bars)
        print(f"  loaded {sym}: {len(series[sym])} bars")

    min_len = min(len(v) for v in series.values()) if series else 0
    if min_len < warmup + 10:
        raise RuntimeError(f"Not enough history: min bars={min_len}, need >{warmup + 10}")

    # Align by taking the last min_len bars of each series
    for sym in symbols:
        series[sym] = series[sym][-min_len:]

    # Temp trades log for PaperBroker (will rewrite clean report)
    tmp_log = ROOT / "data" / ".backtest_trades_tmp.jsonl"
    if tmp_log.exists():
        tmp_log.unlink()

    portfolio = Portfolio(cash_zar=starting, starting_zar=starting)
    broker = PaperBroker(
        cfg["fees"]["taker_pct"],
        tmp_log,
        cfg.get("execution", {}).get("slippage_bps", 0),
    )
    risk = RiskManager(cfg["risk"], cfg["leverage"], cfg["fees"], cfg.get("execution", {}))
    news = NewsSentiment(score=0.0, headline_count=0, top_headlines=[])

    closed: list[dict[str, Any]] = []
    equity_curve = [starting]
    open_meta: dict[str, dict[str, Any]] = {}

    for i in range(warmup, min_len):
        marks: dict[str, float] = {}
        bids: dict[str, float] = {}
        asks: dict[str, float] = {}
        for sym in symbols:
            price = series[sym][i].close
            bid, ask = _synthetic_book(price, assumed_spread)
            marks[sym] = price
            bids[sym] = bid
            asks[sym] = ask

        # Stops / TP using bar high/low for more realistic hits
        for sym in list(portfolio.positions.keys()):
            pos = portfolio.positions[sym]
            bar = series[sym][i]
            hit_sl = bar.low <= pos.stop_loss
            hit_tp = bar.high >= pos.take_profit
            if hit_sl or hit_tp:
                reason = "stop_loss" if hit_sl and (not hit_tp or abs(bar.low - pos.stop_loss) <= abs(bar.high - pos.take_profit)) else "take_profit"
                exit_ref = pos.stop_loss if reason == "stop_loss" else pos.take_profit
                fill = broker.close_long(
                    portfolio, sym, exit_ref, reason=reason, bid_price=bids[sym]
                )
                if fill:
                    closed.append(
                        {
                            "symbol": sym,
                            "pnl": fill.pnl,
                            "reason": reason,
                            "bar": i,
                        }
                    )
                    open_meta.pop(sym, None)

        equity = _equity(portfolio, marks)
        # Use candle calendar day so daily loss halt resets across history
        day = datetime.fromtimestamp(
            series[symbols[0]][i].open_time / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        if portfolio.daily_reset_date != day:
            portfolio.daily_reset_date = day
            portfolio.daily_pnl_zar = 0.0
            portfolio.halted = False

        for sym in symbols:
            window = series[sym][: i + 1]
            tech = analyze(
                window,
                strat["ema_fast"],
                strat["ema_slow"],
                strat["rsi_period"],
                strat["rsi_overbought"],
                strat["rsi_oversold"],
                strat["breakout_period"],
            )
            decision = combine(
                tech,
                news,
                strat["news_block_threshold"],
                strat["news_boost_threshold"],
            )
            regime = classify_regime(
                window,
                strat["ema_fast"],
                strat["ema_slow"],
                int(regime_cfg.get("atr_period", 14)),
                float(regime_cfg.get("range_atr_mult", 0.8)),
                float(regime_cfg.get("trend_ema_pct", 0.002)),
            )
            price = marks[sym]
            spread_bps = assumed_spread

            take_long = False
            size_mult = decision.size_multiplier
            if decision.action == Signal.BUY:
                take_long = allows_new_long(regime.regime, regime_enabled) and (
                    not htf_enabled
                    or htf_trend_up(
                        window,
                        htf_seconds,
                        int(htf_cfg.get("ema_fast", 9)),
                        int(htf_cfg.get("ema_slow", 21)),
                    )
                )
            elif (
                range_enabled
                and decision.action == Signal.HOLD
                and regime.regime == Regime.RANGE
                and tech.rsi <= float(range_cfg.get("rsi_max", 32))
                and tech.low_prior > 0
                and price <= tech.low_prior * (1 + float(range_cfg.get("low_tolerance_pct", 0.005)))
                and news.score > strat["news_block_threshold"]
            ):
                take_long = True
                size_mult = 1.0

            if take_long:
                stop_pct, tp_pct = risk.stop_tp_pcts(regime.atr, price)
                plan = risk.plan_entry(
                    portfolio, sym, equity, size_mult, spread_bps,
                    stop_pct, tp_pct,
                )
                if not plan.approved:
                    continue
                fill = broker.execute_signal(
                    portfolio,
                    sym,
                    Signal.BUY,
                    price,
                    plan.notional_zar,
                    plan.leverage,
                    stop_pct,
                    tp_pct,
                    asks[sym],
                )
                if fill:
                    open_meta[sym] = {"bar": i}

            elif decision.action == Signal.SELL and sym in portfolio.positions:
                if cfg["risk"].get("signal_exit_only_in_profit", False):
                    _, _, est_pnl, _, _ = broker.estimate_close(
                        portfolio.positions[sym], price, bids[sym]
                    )
                    if est_pnl <= 0:
                        continue
                fill = broker.execute_signal(
                    portfolio, sym, Signal.SELL, price, 0, 1, 0, 0, bids[sym]
                )
                if fill:
                    closed.append(
                        {
                            "symbol": sym,
                            "pnl": fill.pnl,
                            "reason": "signal",
                            "bar": i,
                        }
                    )
                    open_meta.pop(sym, None)

        equity_curve.append(_equity(portfolio, marks))

    # Force-close leftovers at last mark
    last_i = min_len - 1
    for sym in list(portfolio.positions.keys()):
        price = series[sym][last_i].close
        bid, _ = _synthetic_book(price, assumed_spread)
        fill = broker.close_long(
            portfolio, sym, price, reason="backtest_eod", bid_price=bid
        )
        if fill:
            closed.append(
                {"symbol": sym, "pnl": fill.pnl, "reason": "backtest_eod", "bar": last_i}
            )

    final_equity = portfolio.cash_zar
    equity_curve.append(final_equity)

    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    net_pnl = sum(t["pnl"] for t in closed)
    win_rate = (len(wins) / len(closed) * 100) if closed else 0.0
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    max_dd = max_drawdown_pct(equity_curve)

    by_symbol: dict[str, dict[str, Any]] = {}
    for t in closed:
        b = by_symbol.setdefault(
            t["symbol"],
            {"symbol": t["symbol"], "trades": 0, "wins": 0, "net_pnl": 0.0, "gp": 0.0, "gl": 0.0},
        )
        b["trades"] += 1
        b["net_pnl"] += t["pnl"]
        if t["pnl"] > 0:
            b["wins"] += 1
            b["gp"] += t["pnl"]
        else:
            b["gl"] += abs(t["pnl"])

    by_reason: dict[str, dict[str, Any]] = {}
    for t in closed:
        r = by_reason.setdefault(
            t["reason"], {"reason": t["reason"], "trades": 0, "wins": 0, "net_pnl": 0.0}
        )
        r["trades"] += 1
        r["net_pnl"] += t["pnl"]
        if t["pnl"] > 0:
            r["wins"] += 1
    per_reason = [
        {
            "reason": r["reason"],
            "trades": r["trades"],
            "wins": r["wins"],
            "net_pnl": round(r["net_pnl"], 2),
            "avg_pnl": round(r["net_pnl"] / r["trades"], 2) if r["trades"] else 0.0,
        }
        for r in sorted(by_reason.values(), key=lambda r: r["net_pnl"])
    ]

    per_pair = []
    for sym, b in sorted(by_symbol.items()):
        pfx = (b["gp"] / b["gl"]) if b["gl"] > 0 else (float("inf") if b["gp"] > 0 else 0.0)
        per_pair.append(
            {
                "symbol": sym,
                "trades": b["trades"],
                "wins": b["wins"],
                "win_rate": round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0.0,
                "net_pnl": round(b["net_pnl"], 2),
                "profit_factor": None if pfx == float("inf") else round(pfx, 2),
                "profit_factor_display": "∞" if pfx == float("inf") else round(pfx, 2),
            }
        )

    # Buy & hold benchmark over the same window (entry at first tradable bar, incl. costs)
    rt_cost_pct = risk.round_trip_cost_pct(assumed_spread)
    bench_pairs = []
    for sym in symbols:
        start_p = series[sym][warmup].close
        end_p = series[sym][-1].close
        raw = (end_p / start_p - 1) * 100
        bench_pairs.append(
            {"symbol": sym, "return_pct": round(raw - rt_cost_pct * 100, 2)}
        )
    bench_equal_weight = round(
        sum(b["return_pct"] for b in bench_pairs) / len(bench_pairs), 2
    ) if bench_pairs else 0.0
    strategy_return_pct = round((final_equity / starting - 1) * 100, 2)

    pass_bar = {
        "net_pnl_positive": net_pnl > 0,
        "profit_factor_ge_1_3": (pf >= 1.3) if pf != float("inf") else True,
        "closed_trades_ge_200": len(closed) >= 200,
        "max_dd_le_15": max_dd <= 15.0,
    }
    pass_bar["all"] = all(pass_bar.values())

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "interval": interval,
        "bars": min_len,
        "days_approx": round(min_len * duration / 86400, 1),
        "symbols": symbols,
        "starting_zar": starting,
        "final_equity_zar": round(final_equity, 2),
        "closed_trades": len(closed),
        "win_rate": round(win_rate, 1),
        "profit_factor": None if pf == float("inf") else round(pf, 2),
        "profit_factor_display": "∞" if pf == float("inf") else round(pf, 2),
        "net_pnl": round(net_pnl, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "assumed_spread_bps": assumed_spread,
        "per_pair": per_pair,
        "per_reason": per_reason,
        "benchmark": {
            "strategy_return_pct": strategy_return_pct,
            "equal_weight_hold_return_pct": bench_equal_weight,
            "per_pair_hold": bench_pairs,
        },
        "pass_bar": pass_bar,
        "regime_enabled": regime_enabled,
        "min_reward_cost_multiple": cfg["risk"].get("min_reward_cost_multiple", 3.0),
    }

    out_path = ROOT / cfg["paths"].get("backtest_report", "data/backtest_last.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if tmp_log.exists():
        tmp_log.unlink()

    return report


def print_report(report: dict[str, Any]) -> None:
    print()
    print("=== Backtest report ===")
    print(f"Bars: {report['bars']} (~{report['days_approx']}d) | {report['interval']}")
    print(f"Pairs: {', '.join(report['symbols'])}")
    print(f"Closed trades: {report['closed_trades']}")
    print(f"Win rate: {report['win_rate']}%")
    print(f"Profit factor: {report['profit_factor_display']}")
    print(f"Net PnL: R{report['net_pnl']:+.2f}")
    print(f"Max DD: {report['max_drawdown_pct']}%")
    print(f"Final equity: R{report['final_equity_zar']:,.2f}")
    print("Per pair:")
    for p in report["per_pair"]:
        print(
            f"  {p['symbol']}: {p['trades']} trades | WR {p['win_rate']}% | "
            f"PF {p['profit_factor_display']} | PnL R{p['net_pnl']:+.2f}"
        )
    bench = report.get("benchmark")
    if bench:
        holds = ", ".join(
            f"{b['symbol']} {b['return_pct']:+.1f}%" for b in bench["per_pair_hold"]
        )
        print(
            f"Benchmark: strategy {bench['strategy_return_pct']:+.2f}% vs "
            f"equal-weight hold {bench['equal_weight_hold_return_pct']:+.2f}% ({holds})"
        )
    if report.get("per_reason"):
        print("Per exit reason:")
        for r in report["per_reason"]:
            print(
                f"  {r['reason']}: {r['trades']} trades | {r['wins']} wins | "
                f"PnL R{r['net_pnl']:+.2f} (avg R{r['avg_pnl']:+.2f})"
            )
    pb = report["pass_bar"]
    print(
        f"Pass bar: {'YES' if pb['all'] else 'NO'} "
        f"(net>0={pb['net_pnl_positive']}, PF>=1.3={pb['profit_factor_ge_1_3']}, "
        f">=200 trades={pb['closed_trades_ge_200']}, DD<=15%={pb['max_dd_le_15']})"
    )
    print(f"Saved -> {ROOT / load_config()['paths'].get('backtest_report', 'data/backtest_last.json')}")
