#!/usr/bin/env python3
"""Crypto Agent Trader — paper trading CLI."""

import argparse
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.agent.runner import run_loop, run_once, show_status


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto Agent Trader (paper mode)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("run", help="Run one analysis + trade cycle")
    sub.add_parser("loop", help="Run continuously (every N seconds from config)")
    sub.add_parser("status", help="Show portfolio status")
    dash = sub.add_parser("dashboard", help="Open live web dashboard")
    dash.add_argument("--port", type=int, default=5055)

    bt = sub.add_parser("backtest", help="Replay strategy on historical Luno candles")
    bt.add_argument("--days", type=int, default=None, help="Approx days of history (default 30)")
    bt.add_argument("--bars", type=int, default=None, help="Exact bar count (overrides --days)")

    sub.add_parser("scout", help="Run the 24/7 news scout loop")

    args = parser.parse_args()

    if args.cmd == "run":
        run_once(verbose=True)
    elif args.cmd == "loop":
        run_loop()
    elif args.cmd == "status":
        show_status()
    elif args.cmd == "dashboard":
        from src.dashboard.app import app

        print(f"Dashboard -> http://0.0.0.0:{args.port}")
        print(f"Local      -> http://127.0.0.1:{args.port}")
        app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)
    elif args.cmd == "backtest":
        from src.backtest.engine import print_report, run_backtest

        print("Running cost-aware backtest (news score fixed at 0)...")
        report = run_backtest(days=args.days, bars=args.bars)
        print_report(report)
    elif args.cmd == "scout":
        from src.data.scout import run_scout_loop

        run_scout_loop()


if __name__ == "__main__":
    main()
