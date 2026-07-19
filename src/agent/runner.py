from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone

from src.analytics.stats import log_equity
from src.config import ROOT, load_config
from src.data.luno import INTERVAL_SECONDS
from src.data.market import fetch_klines, fetch_ticker, fetch_ticker_price
from src.data.news import fetch_news_sentiment
from src.execution.paper_broker import PaperBroker
from src.execution.portfolio import load_portfolio, save_portfolio
from src.risk.manager import RiskManager
from src.strategy.hybrid import combine
from src.strategy.regime import Regime, allows_new_long, classify_regime, htf_trend_up
from src.strategy.technical import Signal, analyze


def _equity_zar(
    portfolio,
    prices: dict[str, float],
    broker: PaperBroker | None = None,
    bids: dict[str, float] | None = None,
) -> float:
    equity = portfolio.cash_zar
    for sym, pos in portfolio.positions.items():
        mark = prices.get(sym, pos.entry_price)
        margin = (pos.quantity * pos.entry_price) / pos.leverage
        if broker:
            _, _, unrealized, _, _ = broker.estimate_close(
                pos, mark, (bids or {}).get(sym)
            )
        else:
            unrealized = (mark - pos.entry_price) * pos.quantity * pos.leverage
        equity += margin + unrealized
    return equity


def run_once(verbose: bool = True) -> dict:
    cfg = load_config()
    state_path = ROOT / cfg["paths"]["state_file"]
    trades_path = ROOT / cfg["paths"]["trades_log"]
    equity_path = ROOT / cfg["paths"].get("equity_log", "data/equity.jsonl")

    starting_zar = float(cfg["capital"]["starting_zar"])
    portfolio = load_portfolio(state_path, starting_zar)
    broker = PaperBroker(
        cfg["fees"]["taker_pct"],
        trades_path,
        cfg.get("execution", {}).get("slippage_bps", 0),
    )
    risk = RiskManager(cfg["risk"], cfg["leverage"], cfg["fees"], cfg.get("execution", {}))

    risk.reset_daily_if_needed(portfolio)
    news = fetch_news_sentiment(cfg["news"]["feeds"])
    strat_cfg = cfg["strategy"]
    regime_cfg = strat_cfg.get("regime") or {}
    regime_enabled = bool(regime_cfg.get("enabled", True))

    symbols = list(cfg["trading"]["symbols"])
    watch_syms = [
        s for s in (cfg["trading"].get("watch_symbols") or []) if s not in symbols
    ]
    # Manage leftover positions on dropped pairs (stops/exits only)
    manage_symbols = list(
        dict.fromkeys(symbols + watch_syms + list(portfolio.positions.keys()))
    )

    prices: dict[str, float] = {}
    asks: dict[str, float] = {}
    bids: dict[str, float] = {}
    for sym in manage_symbols:
        try:
            ticker = fetch_ticker(sym)
            prices[sym] = ticker["last"]
            asks[sym] = ticker["ask"]
            bids[sym] = ticker["bid"]
        except Exception as exc:
            if verbose:
                print(f"  WARN ticker {sym}: {exc}")

    stop_fills = broker.check_stops(portfolio, prices, bids)
    if verbose and stop_fills:
        for f in stop_fills:
            print(f"  STOP {f.symbol} @ R{f.price:.2f} PnL=R{f.pnl:+.2f}")

    if news.score <= strat_cfg["news_block_threshold"] - 0.2:
        for sym in list(portfolio.positions.keys()):
            if sym not in prices:
                continue
            fill = broker.close_long(
                portfolio,
                sym,
                prices[sym],
                reason="news_panic",
                bid_price=bids.get(sym),
            )
            if verbose and fill:
                print(f"  NEWS EXIT {sym} @ R{fill.price:.2f} PnL=R{fill.pnl:+.2f}")

    equity = _equity_zar(portfolio, prices, broker, bids)
    results = []

    for symbol in symbols + watch_syms:
        is_watch = symbol in watch_syms
        candles = fetch_klines(symbol, cfg["trading"]["interval"], limit=100)
        tech = analyze(
            candles,
            strat_cfg["ema_fast"],
            strat_cfg["ema_slow"],
            strat_cfg["rsi_period"],
            strat_cfg["rsi_overbought"],
            strat_cfg["rsi_oversold"],
            strat_cfg["breakout_period"],
        )
        symbol_news = replace(news, score=news.score_for(symbol))
        decision = combine(
            tech,
            symbol_news,
            strat_cfg["news_block_threshold"],
            strat_cfg["news_boost_threshold"],
        )
        regime = classify_regime(
            candles,
            strat_cfg["ema_fast"],
            strat_cfg["ema_slow"],
            int(regime_cfg.get("atr_period", 14)),
            float(regime_cfg.get("range_atr_mult", 0.8)),
            float(regime_cfg.get("trend_ema_pct", 0.002)),
        )
        price = prices.get(symbol)
        if price is None:
            results.append(
                {
                    "symbol": symbol,
                    "price": None,
                    "signal": "ERROR",
                    "reason": "no ticker",
                    "regime": regime.regime.value,
                    "watch": is_watch,
                }
            )
            continue

        spread_bps = ((asks[symbol] - bids[symbol]) / price * 10_000) if price else 0

        row = {
            "symbol": symbol,
            "price": price,
            "signal": decision.action.value,
            "reason": decision.reason,
            "rsi": tech.rsi,
            "news": symbol_news.score,
            "candles": len(candles),
            "spread_bps": spread_bps,
            "regime": regime.regime.value,
            "regime_reason": regime.reason,
            "watch": is_watch,
        }
        results.append(row)

        if verbose:
            watch_tag = " | WATCH-ONLY" if is_watch else ""
            print(
                f"  {symbol} R{price:,.2f} | {decision.action.value} | "
                f"{regime.regime.value} | RSI {tech.rsi:.1f} | "
                f"spread {spread_bps:.1f}bps | news {symbol_news.score:+.2f} | {decision.reason}{watch_tag}"
            )

        take_long = False
        size_mult = decision.size_multiplier
        if is_watch:
            pass  # watch-only: never open positions; stops/exits still manage leftovers
        elif decision.action == Signal.BUY:
            if not allows_new_long(regime.regime, regime_enabled):
                if verbose:
                    print(f"    -> SKIP regime {regime.regime.value} blocks new long")
                continue
            take_long = True
            htf_cfg = strat_cfg.get("htf_filter") or {}
            if bool(htf_cfg.get("enabled", False)):
                htf_interval = htf_cfg.get("interval", "4h")
                try:
                    htf_candles = fetch_klines(symbol, htf_interval, limit=80)
                except Exception:
                    htf_candles = []
                if not htf_trend_up(
                    htf_candles,
                    INTERVAL_SECONDS.get(htf_interval, 14400),
                    int(htf_cfg.get("ema_fast", 9)),
                    int(htf_cfg.get("ema_slow", 21)),
                ):
                    if verbose:
                        print(f"    -> SKIP {htf_interval} trend is down")
                    continue
        else:
            range_cfg = strat_cfg.get("range_mode") or {}
            if (
                bool(range_cfg.get("enabled", False))
                and decision.action == Signal.HOLD
                and regime.regime == Regime.RANGE
                and tech.rsi <= float(range_cfg.get("rsi_max", 32))
                and tech.low_prior > 0
                and price <= tech.low_prior * (1 + float(range_cfg.get("low_tolerance_pct", 0.005)))
                and symbol_news.score > strat_cfg["news_block_threshold"]
            ):
                take_long = True
                size_mult = 1.0
                if verbose:
                    print(f"    -> RANGE BUY setup (RSI {tech.rsi:.1f} at prior low)")

        if take_long:
            max_spread = cfg.get("execution", {}).get("max_spread_bps", float("inf"))
            if spread_bps > max_spread:
                if verbose:
                    print(
                        f"    -> SKIP spread {spread_bps:.1f}bps exceeds "
                        f"{max_spread:.1f}bps limit"
                    )
                continue
            stop_pct, tp_pct = risk.stop_tp_pcts(regime.atr, price)
            plan = risk.plan_entry(
                portfolio, symbol, equity, size_mult, spread_bps,
                stop_pct, tp_pct,
            )
            if plan.approved:
                broker.execute_signal(
                    portfolio,
                    symbol,
                    Signal.BUY,
                    price,
                    plan.notional_zar,
                    plan.leverage,
                    stop_pct,
                    tp_pct,
                    asks[symbol],
                )
                if verbose:
                    print(
                        f"    -> OPEN {symbol} R{plan.notional_zar:.2f} "
                        f"@ {plan.leverage}x (ask + spread/slippage/fee)"
                    )
            elif verbose:
                print(f"    -> SKIP {plan.reason}")

        elif decision.action == Signal.SELL and symbol in portfolio.positions:
            if cfg["risk"].get("signal_exit_only_in_profit", False):
                _, _, est_pnl, _, _ = broker.estimate_close(
                    portfolio.positions[symbol], price, bids.get(symbol)
                )
                if est_pnl <= 0:
                    if verbose:
                        print(
                            f"    -> HOLD {symbol} (signal exit skipped at est PnL R{est_pnl:+.2f}; stop manages risk)"
                        )
                    continue
            fill = broker.execute_signal(
                portfolio, symbol, Signal.SELL, price, 0, 1, 0, 0, bids[symbol]
            )
            if verbose and fill:
                print(f"    -> CLOSE {symbol} PnL=R{fill.pnl:+.2f}")

    # Exit leftover positions on symbols no longer in the trading universe on SELL/stops only
    # (stops already handled; optional: no new signal path for them)

    equity = _equity_zar(portfolio, prices, broker, bids)
    save_portfolio(state_path, portfolio)
    log_equity(equity_path, equity, {"positions": len(portfolio.positions)})

    pnl_total = equity - portfolio.starting_zar
    pnl_pct = (pnl_total / portfolio.starting_zar) * 100 if portfolio.starting_zar else 0

    summary = {
        "equity_zar": round(equity, 2),
        "pnl_zar": round(pnl_total, 2),
        "pnl_pct": round(pnl_pct, 2),
        "positions": len(portfolio.positions),
        "halted": portfolio.halted,
        "news_score": news.score,
        "results": results,
    }

    if verbose:
        print()
        print(
            f"Portfolio: R{equity:,.2f} | "
            f"PnL R{pnl_total:+.2f} ({pnl_pct:+.1f}%) | "
            f"Open {len(portfolio.positions)} | "
            f"{'HALTED' if portfolio.halted else 'ACTIVE'} | "
            f"Luno paper"
        )

    return summary


def run_loop() -> None:
    cfg = load_config()
    interval = cfg["trading"]["loop_seconds"]
    print(f"Crypto Agent Trader — LUNO PAPER | loop every {interval}s | Ctrl+C to stop")
    print(f"Capital: R{cfg['capital']['starting_zar']}")
    print(f"Pairs: {', '.join(cfg['trading']['symbols'])}")
    print()

    while True:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"--- Tick {ts} ---")
        try:
            run_once(verbose=True)
        except Exception as exc:
            print(f"ERROR: {exc}")
        print()
        time.sleep(interval)


def show_status() -> None:
    cfg = load_config()
    state_path = ROOT / cfg["paths"]["state_file"]
    starting_zar = float(cfg["capital"]["starting_zar"])
    portfolio = load_portfolio(state_path, starting_zar)

    prices = {}
    for sym in list(dict.fromkeys(cfg["trading"]["symbols"] + list(portfolio.positions.keys()))):
        try:
            prices[sym] = fetch_ticker_price(sym)
        except Exception:
            pass

    equity = _equity_zar(portfolio, prices) if prices else portfolio.cash_zar
    pnl = equity - portfolio.starting_zar

    print("=== Crypto Agent Trader (LUNO PAPER) ===")
    print(f"Equity:    R{equity:,.2f}")
    print(f"Starting:  R{portfolio.starting_zar:,.2f}")
    print(f"PnL:       R{pnl:+.2f}")
    print(f"Cash:      R{portfolio.cash_zar:,.2f}")
    print(f"Trades:    {portfolio.trade_count}")
    print(f"Status:    {'HALTED' if portfolio.halted else 'ACTIVE'}")
    print()
    if portfolio.positions:
        print("Open positions:")
        for sym, pos in portfolio.positions.items():
            mark = prices.get(sym, pos.entry_price)
            unr = (mark - pos.entry_price) * pos.quantity * pos.leverage
            print(
                f"  {sym} LONG {pos.quantity:.6f} @ R{pos.entry_price:.2f} "
                f"(mark R{mark:.2f}) {pos.leverage}x | SL R{pos.stop_loss:.2f} TP R{pos.take_profit:.2f} "
                f"| uPnL R{unr:+.2f}"
            )
    else:
        print("No open positions.")
