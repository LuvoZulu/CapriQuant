"""
CapriQuant - Strict Backtester v2 (Recommended Accurate Version)

This is the proper backtester that uses the full, real engine
(app/backtest/replay.py + the strict v2 structure logic).

Focus: Currently configured for easy XAUUSD (Gold) M5 + M15 testing.

Usage:
    cd python-backend
    python backtest_strict.py

Note: A (Strict recent BOS requirement) and C (Higher Timeframe bias)
are active in the engine when using this + the latest confluence code.
"""

import sys
from pathlib import Path
import pandas as pd

sys.path.append(str(Path(__file__).parent))

from utils.load_mt5_data import load_mt5_csv
from app.backtest.replay import run_backtest


def run_strict_backtest(symbol: str, timeframe: str, df: pd.DataFrame,
                        starting_equity: float = 200.0,
                        risk_per_trade: float = 1.8,
                        min_confluence: float = 0.72,
                        print_trades: bool = True):
    """
    Runs the full accurate backtester with the strict engine.
    """
    print(f"\n{'='*80}")
    print(f"STRICT BACKTEST: {symbol} {timeframe}")
    print(f"Bars: {len(df):,} | Period: {df['timestamp'].min().date()} → {df['timestamp'].max().date()}")
    print(f"Min Confluence Threshold: {min_confluence}")
    print(f"{'='*80}")

    result = run_backtest(
        df,
        symbol=symbol,
        timeframe=timeframe,
        starting_equity=starting_equity,
        risk_per_trade=risk_per_trade,
        min_confluence_score=min_confluence,
        step=6,
    )

    summary = result.get("summary", {})
    trades = result.get("trades", [])

    print(f"\n>>> RESULT SUMMARY: {symbol} {timeframe}")
    print(f"    Trades Taken   : {summary.get('total_trades', 0)}")
    print(f"    Win Rate       : {summary.get('win_rate', 0)}%")
    print(f"    Expectancy     : {summary.get('expectancy_r', 0)} R")
    print(f"    Profit Factor  : {summary.get('profit_factor', 0)}")
    print(f"    Final Equity   : R{summary.get('final_equity', starting_equity):.0f}")
    print(f"    Return         : {summary.get('return_pct', 0)}%")

    if print_trades and trades:
        print(f"\n--- Individual Trades ({len(trades)}) ---")
        for i, t in enumerate(trades[:25]):  # Limit output
            entry_time = str(t.get('entry_time', ''))[:19]
            direction = t.get('direction', '')
            r_mult = t.get('r_multiple', 0)
            setup = str(t.get('setup', ''))[:30]
            equity = t.get('equity', 0)
            print(f"{i+1:2}. {entry_time} | {direction} | R={r_mult:+5.2f} | {setup} | Eq: {equity:.0f}")

        if len(trades) > 25:
            print(f"... and {len(trades)-25} more trades ...")

        # Show worst and best trades safely
        try:
            sorted_trades = sorted(trades, key=lambda x: x.get('r_multiple', 0))
            worst = [round(t.get('r_multiple', 0), 2) for t in sorted_trades[:3]]
            best = [round(t.get('r_multiple', 0), 2) for t in sorted_trades[-3:]]
            print(f"\nWorst 3 trades R-multiples: {worst}")
            print(f"Best  3 trades R-multiples: {best}")
        except Exception as e:
            print(f"(Could not compute best/worst trades: {e})")

    return summary, trades


def main():
    print("=" * 85)
    print("CAPRIQUANT STRICT BACKTESTER v2 (Post-Tuning) - ACCURATE VERSION")
    print("Using the full engine + strict v2 structure logic")
    print("=" * 85)

    testing_dir = Path(__file__).parent.parent / "testing"

    # ==================================================================
    # DEDICATED GOLD (XAUUSD) M5 + M15 TESTING
    # Philosophy: Quality structure + trend participation (not pure scalping).
    # Target frequency: Significantly more than 6 trades / 17 months.
    # A (BOS awareness) + C (HTF bias) are respected but not total killers.
    # ==================================================================
    tests = [
        ("XAUUSDm_M15_202501012300_202605292045.csv", "XAUUSD", "M15", 0.55),
    ]

    # Lower the numbers above (e.g. 0.50) for more trades.
    # Raise them (e.g. 0.68) for higher quality / fewer trades.

    all_results = []

    for filename, symbol, tf, min_conf in tests:
        filepath = testing_dir / filename
        if not filepath.exists():
            print(f"Missing file: {filename}")
            continue

        df = load_mt5_csv(filepath, symbol=symbol)

        summary, trades = run_strict_backtest(
            symbol=symbol,
            timeframe=tf,
            df=df,
            starting_equity=200.0,
            risk_per_trade=1.8,
            min_confluence=min_conf,
            print_trades=True
        )

        all_results.append({
            "symbol": symbol,
            "tf": tf,
            **summary
        })

    # Final Summary Table
    print("\n\n" + "=" * 85)
    print("STRICT ENGINE v2 - FINAL COMPARISON TABLE")
    print("=" * 85)
    print(f"{'Symbol':<8} {'TF':<5} {'Trades':>7} {'Win%':>7} {'Expect(R)':>10} {'PF':>6} {'Final Eq':>10}")
    print("-" * 85)

    for r in all_results:
        print(f"{r['symbol']:<8} {r['tf']:<5} {r['total_trades']:>7} {r['win_rate']:>6.1f}% "
              f"{r['expectancy_r']:>10.3f} {r['profit_factor']:>6.2f} {r['final_equity']:>10.0f}")

    print("\n" + "=" * 85)
    print("INTERPRETATION AFTER STRICT v2 CHANGES:")
    print("- Much lower trade count = filters are working (good)")
    print("- Higher win rate than before is a positive signal")
    print("- We are still looking for positive expectancy overall")
    print("- Look at the individual trade R-multiples printed above for patterns")
    print("=" * 85)


if __name__ == "__main__":
    main()
