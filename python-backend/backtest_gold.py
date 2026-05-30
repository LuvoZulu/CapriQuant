"""
CapriQuant - Dedicated Gold (XAUUSD) Backtester

This script is specifically for testing XAUUSD on M5 and M15 with the strict engine.

It supports running:
- M5 alone
- M15 alone
- Both (recommended)

It also has basic support for applying Higher Timeframe (HTF) bias when testing M5
(Option C from earlier discussion).

Usage:
    cd python-backend
    python backtest_gold.py
"""

import sys
from pathlib import Path
import pandas as pd

sys.path.append(str(Path(__file__).parent))

from utils.load_mt5_data import load_mt5_csv
from app.backtest.replay import run_backtest
from app.features.structure import compute_market_structure


def get_htf_bias(df_htf: pd.DataFrame, symbol: str, tf: str) -> str:
    """
    Simple Higher Timeframe bias calculator.
    Returns: "BULLISH", "BEARISH", or "NEUTRAL"
    """
    try:
        ms = compute_market_structure(df_htf.tail(220), symbol=symbol, timeframe=tf)
        return ms.bias
    except Exception:
        return "NEUTRAL"


def run_gold_backtest(timeframe: str, min_confluence: float = 0.72, use_htf_bias: bool = False):
    testing_dir = Path(__file__).parent.parent / "testing"

    if timeframe == "M5":
        filename = "XAUUSDm_M5_202501012305_202605292055.csv"
        htf_filename = "XAUUSDm_M15_202501012300_202605292045.csv"
    elif timeframe == "M15":
        filename = "XAUUSDm_M15_202501012300_202605292045.csv"
        htf_filename = None
    else:
        print(f"Unsupported timeframe: {timeframe}")
        return

    filepath = testing_dir / filename
    if not filepath.exists():
        print(f"File not found: {filepath}")
        return

    df = load_mt5_csv(filepath, symbol="XAUUSD")
    print(f"\n{'='*85}")
    print(f"XAUUSD {timeframe} | Bars: {len(df):,}")
    print(f"{'='*85}")

    htf_bias = "NEUTRAL"
    if use_htf_bias and timeframe == "M5" and htf_filename:
        htf_path = testing_dir / htf_filename
        if htf_path.exists():
            df_htf = load_mt5_csv(htf_path, symbol="XAUUSD")
            htf_bias = get_htf_bias(df_htf, "XAUUSD", "M15")
            print(f"HTF (M15) Bias detected: {htf_bias}")

    # Run the backtest
    result = run_backtest(
        df,
        symbol="XAUUSD",
        timeframe=timeframe,
        starting_equity=200.0,
        risk_per_trade=1.8,
        min_confluence_score=min_confluence,
        step=6,
    )

    summary = result.get("summary", {})
    trades = result.get("trades", [])

    print(f"\n>>> XAUUSD {timeframe} RESULT")
    print(f"    Trades      : {summary.get('total_trades', 0)}")
    print(f"    Win Rate    : {summary.get('win_rate', 0)}%")
    print(f"    Expectancy  : {summary.get('expectancy_r', 0)} R")
    print(f"    Profit Factor: {summary.get('profit_factor', 0)}")
    print(f"    Final Equity: R{summary.get('final_equity', 200):.0f}")
    print(f"    Return      : {summary.get('return_pct', 0)}%")

    # Basic HTF bias filter post-processing (Option C)
    if use_htf_bias and timeframe == "M5" and htf_bias != "NEUTRAL" and trades:
        filtered_trades = []
        for t in trades:
            trade_dir = t.get("direction")
            if (htf_bias == "BULLISH" and trade_dir == "BUY") or (htf_bias == "BEARISH" and trade_dir == "SELL"):
                filtered_trades.append(t)
            # else: we drop the trade because it goes against HTF bias

        if filtered_trades:
            wins = [t for t in filtered_trades if t.get("r_multiple", 0) > 0]
            win_rate = len(wins) / len(filtered_trades) * 100
            expectancy = sum(t.get("r_multiple", 0) for t in filtered_trades) / len(filtered_trades)
            print(f"\n    [HTF Bias Filter Applied - {htf_bias}]")
            print(f"    Trades after HTF filter : {len(filtered_trades)}")
            print(f"    Win Rate after filter   : {win_rate:.1f}%")
            print(f"    Expectancy after filter : {expectancy:.3f} R")
        else:
            print("\n    [HTF Bias Filter] All trades filtered out by M15 bias.")

    return summary


def main():
    print("=" * 90)
    print("CAPRIQUANT - DEDICATED XAUUSD (GOLD) BACKTESTER")
    print("Testing M5 and M15 with Strict Engine + BOS + HTF Bias options")
    print("=" * 90)

    # === Run M5 ===
    run_gold_backtest("M5", min_confluence=0.72, use_htf_bias=True)

    # === Run M15 ===
    run_gold_backtest("M15", min_confluence=0.70, use_htf_bias=False)

    print("\n" + "=" * 90)
    print("Done. Compare M5 (with HTF bias) vs M15 results above.")
    print("You can edit min_confluence and use_htf_bias in the script to experiment.")
    print("=" * 90)


if __name__ == "__main__":
    main()
