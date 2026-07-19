from __future__ import annotations

import json
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template

from src.agent.runner import _equity_zar
from src.analytics.stats import compute_stats, load_backtest_report
from src.config import ROOT, load_config
from src.data.market import fetch_klines, fetch_ticker, fetch_wallet_summary
from src.data.news import fetch_news_sentiment
from src.execution.paper_broker import PaperBroker
from src.execution.portfolio import load_portfolio
from src.strategy.hybrid import combine
from src.strategy.regime import classify_regime
from src.strategy.technical import analyze

app = Flask(
    __name__,
    template_folder=str(ROOT / "templates"),
    static_folder=str(ROOT / "static"),
)


def _recent_trades(limit: int = 20) -> list[dict]:
    path = ROOT / load_config()["paths"]["trades_log"]
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    trades = []
    for line in lines[-limit:]:
        try:
            trades.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    trades.reverse()
    return trades


def build_snapshot() -> dict:
    cfg = load_config()
    starting_zar = float(cfg["capital"]["starting_zar"])
    portfolio = load_portfolio(ROOT / cfg["paths"]["state_file"], starting_zar)
    strat = cfg["strategy"]
    regime_cfg = strat.get("regime") or {}

    prices: dict[str, float] = {}
    bids: dict[str, float] = {}
    signals: list[dict] = []
    news = fetch_news_sentiment(cfg["news"]["feeds"])

    trade_syms = list(cfg["trading"]["symbols"])
    watch_syms = [
        s for s in (cfg["trading"].get("watch_symbols") or []) if s not in trade_syms
    ]
    for symbol in trade_syms + watch_syms:
        is_watch = symbol in watch_syms
        try:
            ticker = fetch_ticker(symbol)
            price = ticker["last"]
            prices[symbol] = price
            bids[symbol] = ticker["bid"]
            candles = fetch_klines(symbol, cfg["trading"]["interval"], limit=100)
            tech = analyze(
                candles,
                strat["ema_fast"],
                strat["ema_slow"],
                strat["rsi_period"],
                strat["rsi_overbought"],
                strat["rsi_oversold"],
                strat["breakout_period"],
            )
            decision = combine(
                tech, news, strat["news_block_threshold"], strat["news_boost_threshold"]
            )
            regime = classify_regime(
                candles,
                strat["ema_fast"],
                strat["ema_slow"],
                int(regime_cfg.get("atr_period", 14)),
                float(regime_cfg.get("range_atr_mult", 0.8)),
                float(regime_cfg.get("trend_ema_pct", 0.002)),
            )
            series = [
                {"t": c.open_time, "c": c.close}
                for c in candles[-60:]
            ]
            signals.append(
                {
                    "symbol": symbol,
                    "price": round(price, 2),
                    "signal": decision.action.value,
                    "reason": decision.reason,
                    "rsi": round(tech.rsi, 1),
                    "ema_fast": round(tech.ema_fast, 2),
                    "ema_slow": round(tech.ema_slow, 2),
                    "spread_bps": round(
                        (ticker["ask"] - ticker["bid"]) / price * 10_000, 1
                    ),
                    "regime": regime.regime.value,
                    "regime_reason": regime.reason,
                    "candles": len(candles),
                    "series": series,
                    "watch": is_watch,
                }
            )
        except Exception as exc:
            signals.append(
                {
                    "symbol": symbol,
                    "price": None,
                    "signal": "ERROR",
                    "reason": str(exc),
                    "rsi": None,
                    "ema_fast": None,
                    "ema_slow": None,
                    "regime": None,
                    "candles": 0,
                    "watch": is_watch,
                }
            )

    # Prices for leftover positions not in universe
    for sym in portfolio.positions:
        if sym not in prices:
            try:
                t = fetch_ticker(sym)
                prices[sym] = t["last"]
                bids[sym] = t["bid"]
            except Exception:
                pass

    broker = PaperBroker(
        cfg["fees"]["taker_pct"],
        ROOT / cfg["paths"]["trades_log"],
        cfg.get("execution", {}).get("slippage_bps", 0),
    )
    equity = (
        _equity_zar(portfolio, prices, broker, bids)
        if prices
        else portfolio.cash_zar
    )
    pnl = equity - portfolio.starting_zar
    pnl_pct = (pnl / portfolio.starting_zar * 100) if portfolio.starting_zar else 0

    positions = []
    for sym, pos in portfolio.positions.items():
        mark = prices.get(sym, pos.entry_price)
        fill, exit_fee, unr, spread_cost, slippage_cost = broker.estimate_close(
            pos, mark, bids.get(sym)
        )
        positions.append(
            {
                "symbol": sym,
                "quantity": round(pos.quantity, 8),
                "entry": round(pos.entry_price, 2),
                "mark": round(mark, 2),
                "exit_fill": round(fill, 2),
                "leverage": pos.leverage,
                "stop_loss": round(pos.stop_loss, 2),
                "take_profit": round(pos.take_profit, 2),
                "unrealized_pnl": round(unr, 2),
                "estimated_exit_fee": round(exit_fee, 2),
                "estimated_exit_spread": round(spread_cost, 2),
                "estimated_exit_slippage": round(slippage_cost, 2),
                "opened_at": pos.opened_at,
            }
        )

    wallet = fetch_wallet_summary()
    stats = compute_stats(
        ROOT / cfg["paths"]["trades_log"],
        ROOT / cfg["paths"].get("equity_log", "data/equity.jsonl"),
        starting_zar,
    )
    backtest = load_backtest_report(
        ROOT / cfg["paths"].get("backtest_report", "data/backtest_last.json")
    )

    return {
        "mode": "PAPER",
        "exchange": "luno",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "equity_zar": round(equity, 2),
        "starting_zar": round(portfolio.starting_zar, 2),
        "cash_zar": round(portfolio.cash_zar, 2),
        "pnl_zar": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "daily_pnl_zar": round(portfolio.daily_pnl_zar, 2),
        "taker_fee_pct": cfg["fees"]["taker_pct"] * 100,
        "slippage_bps": cfg.get("execution", {}).get("slippage_bps", 0),
        "min_reward_cost_multiple": cfg["risk"].get("min_reward_cost_multiple", 3.0),
        "trade_count": portfolio.trade_count,
        "halted": portfolio.halted,
        "max_leverage": cfg["leverage"]["max"],
        "news_score": round(news.score, 2),
        "news_headlines": news.top_headlines,
        "news_source": news.source,
        "news_age_min": news.age_min,
        "news_count": news.headline_count,
        "news_per_coin": news.per_coin,
        "signals": signals,
        "positions": positions,
        "trades": _recent_trades(15),
        "wallet": wallet,
        "stats": stats,
        "backtest": backtest,
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/snapshot")
def api_snapshot():
    try:
        return jsonify(build_snapshot())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def main() -> None:
    print("Dashboard -> http://0.0.0.0:5055 (LAN)")
    print("Local     -> http://127.0.0.1:5055")
    app.run(host="0.0.0.0", port=5055, debug=False, threaded=True)


if __name__ == "__main__":
    main()
