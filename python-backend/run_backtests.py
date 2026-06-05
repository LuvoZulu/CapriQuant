"""
CapriQuant - Easy Backtest Runner

Usage:
    cd python-backend
    python run_backtests.py

This script will:
1. Automatically find all your exported files in ../testing/
2. Load them correctly
3. Run the structure engine backtester on the main symbols/timeframes
4. Print clear performance summaries

You can also run specific symbols manually.
"""

import sys
from pathlib import Path
import pandas as pd

# Add project root to path
sys.path.append(str(Path(__file__).parent))

from utils.load_mt5_data import load_mt5_csv, load_all_testing_data
from app.backtest.replay import run_backtest
from app.config import get_settings


def main():
    print("=" * 70)
    print("CAPRIQUANT STRUCTURE ENGINE - BACKTEST RUNNER")
    print("=" * 70)

    testing_dir = Path(__file__).parent.parent / "testing"

    if not testing_dir.exists():
        print(f"ERROR: Testing folder not found at: {testing_dir}")
        return

    print(f"\nScanning for data in: {testing_dir}\n")

    all_data = load_all_testing_data(testing_dir)

    if not all_data:
        print("No valid data files found.")
        return

    # Prioritize the most useful runs for your goal
    priority_runs = [
        ("XAUUSD", "M5"),
        ("XAUUSD", "M15"),
        ("NAS100", "M5"),
        ("US30", "M5"),
        ("GER30", "M5"),
        ("XAUUSD", "M1"),   # M1 is noisy but interesting for scalping
    ]

    results_summary = []

    for symbol, tf in priority_runs:
        key = (symbol, tf)
        if key not in all_data:
            print(f"\nSkipping {symbol} {tf} - no data file found.")
            continue

        df = all_data[key]
        print(f"\n{'='*70}")
        print(f"RUNNING BACKTEST: {symbol} {tf}")
        print(f"Bars: {len(df)} | Range: {df['timestamp'].min()} to {df['timestamp'].max()}")
        print(f"{'='*70}")

        try:
            s = get_settings()
            result = run_backtest(
                df,
                symbol=symbol,
                timeframe=tf,
                starting_equity=s.risk_starting_equity,
                risk_per_trade=s.risk_base_pct,
                min_confluence_score=0.68,
                spread_points=0.30,          # realistic round-turn cost
                use_risk_manager=True,       # dynamic + hard circuits
            )

            summary = result.get("summary", {})
            results_summary.append({
                "symbol": symbol,
                "tf": tf,
                "trades": summary.get("total_trades", 0),
                "win_rate": summary.get("win_rate", 0),
                "expectancy_r": summary.get("expectancy_r", 0),
                "profit_factor": summary.get("profit_factor", 0),
                "final_equity": summary.get("final_equity", 200),
            })

        except Exception as e:
            print(f"ERROR running backtest for {symbol} {tf}: {e}")
            import traceback
            traceback.print_exc()

    # Final summary table
    print("\n\n" + "=" * 70)
    print("BACKTEST SUMMARY - ALL RUNS")
    print("=" * 70)
    print(f"{'Symbol':<10} {'TF':<6} {'Trades':>8} {'Win%':>8} {'Expect(R)':>10} {'PF':>6} {'Final Eq':>10}")
    print("-" * 70)

    for r in results_summary:
        print(f"{r['symbol']:<10} {r['tf']:<6} {r['trades']:>8} {r['win_rate']:>7.1f}% "
              f"{r['expectancy_r']:>10.2f} {r['profit_factor']:>6.2f} {r['final_equity']:>10.0f}")

    print("\nInterpretation guide:")
    print("  - Expectancy (R) > 0.15 is decent for a structure system")
    print("  - Profit Factor > 1.3 is acceptable")
    print("  - Win rate for these setups is often 45-58% — focus on R-multiple")
    print("  - Look at consistency across symbols and timeframes")

    print("\nNext step: If you see positive expectancy, we can tune parameters and add more filters.")


if __name__ == "__main__":
    main()
