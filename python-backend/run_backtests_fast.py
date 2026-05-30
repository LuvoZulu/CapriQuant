"""
CapriQuant - FAST Backtest Runner (Recommended)

This version is much more practical for large datasets.
It steps every 8 bars and uses smarter structure computation.

Usage:
    cd python-backend
    python run_backtests_fast.py
"""

import sys
from pathlib import Path
import pandas as pd

sys.path.append(str(Path(__file__).parent))

from utils.load_mt5_data import load_mt5_csv
from app.features.structure import compute_market_structure
from app.engine.confluence import evaluate_setups


def fast_backtest(df: pd.DataFrame, symbol: str, timeframe: str, 
                  starting_equity: float = 200.0, risk_per_trade: float = 1.8,
                  min_score: float = 0.67, step: int = 8):
    """
    Faster version of the backtester.
    """
    df = df.sort_values("timestamp").reset_index(drop=True)
    equity = starting_equity
    trades = []

    window = 180
    print(f"  Fast backtest {symbol} {timeframe} | {len(df)} bars | step={step}")

    for end in range(window, len(df) - 60, step):
        window_df = df.iloc[end - window : end].copy()
        try:
            ms = compute_market_structure(window_df, symbol=symbol, timeframe=timeframe)
        except Exception:
            continue

        setups = evaluate_setups(ms)
        if not setups:
            continue

        best = setups[0]
        if best.score < min_score:
            continue

        # Simulate next bar
        entry_bar = df.iloc[end]
        entry_price = float(entry_bar["open"])
        stop = best.stop_suggestion
        tp1 = best.tp1
        tp2 = best.tp2

        future = df.iloc[end+1 : min(end+55, len(df))]
        exit_price = entry_price
        exit_reason = "timeout"

        for _, bar in future.iterrows():
            if best.direction == "BUY":
                if bar["low"] <= stop:
                    exit_price, exit_reason = stop, "stop"
                    break
                if bar["high"] >= tp2:
                    exit_price, exit_reason = tp2, "tp2"
                    break
                if bar["high"] >= tp1:
                    exit_price, exit_reason = tp1, "tp1"
                    break
            else:
                if bar["high"] >= stop:
                    exit_price, exit_reason = stop, "stop"
                    break
                if bar["low"] <= tp2:
                    exit_price, exit_reason = tp2, "tp2"
                    break
                if bar["low"] <= tp1:
                    exit_price, exit_reason = tp1, "tp1"
                    break

        risk_dist = abs(entry_price - stop)
        if risk_dist < 0.0001:
            continue

        reward = abs(exit_price - entry_price)
        r_multiple = (reward / risk_dist) * (1 if best.direction == "BUY" else -1)
        if best.direction == "SELL" and exit_reason == "stop":
            r_multiple = -r_multiple

        pnl = equity * (risk_per_trade / 100.0) * r_multiple
        equity += pnl

        trades.append({
            "direction": best.direction,
            "setup": best.name,
            "r": round(r_multiple, 2),
            "pnl": round(pnl, 2),
            "equity": round(equity, 2),
            "confluences": len(best.confluences)
        })

    if not trades:
        return {"trades": 0, "win_rate": 0, "expectancy": 0, "final_equity": equity}

    wins = [t for t in trades if t["r"] > 0]
    win_rate = len(wins) / len(trades) * 100
    expectancy = sum(t["r"] for t in trades) / len(trades)
    profit_factor = (sum(t["r"] for t in wins) / abs(sum(t["r"] for t in trades if t["r"] <= 0))) if any(t["r"] <= 0 for t in trades) else 99

    return {
        "trades": len(trades),
        "win_rate": round(win_rate, 1),
        "expectancy_r": round(expectancy, 3),
        "profit_factor": round(profit_factor, 2),
        "final_equity": round(equity, 0),
        "return_pct": round((equity - starting_equity) / starting_equity * 100, 1)
    }


def main():
    print("=" * 75)
    print("CAPRIQUANT STRUCTURE ENGINE - FAST BACKTEST (Real Data)")
    print("=" * 75)

    testing = Path(__file__).parent.parent / "testing"

    symbols_to_test = [
        ("XAUUSDm_M5_202501012305_202605292055.csv", "XAUUSD", "M5"),
        ("XAUUSDm_M15_202501012300_202605292045.csv", "XAUUSD", "M15"),
        ("USTECm_M5_202602172125_202605292054.csv", "NAS100", "M5"),
        ("US30m_M5_202602172100_202605292054.csv", "US30", "M5"),
        ("DE30m_M5_202501020015_202605291955.csv", "GER30", "M5"),
    ]

    results = []

    for filename, symbol, tf in symbols_to_test:
        filepath = testing / filename
        if not filepath.exists():
            print(f"Missing: {filename}")
            continue

        print(f"\n{'='*75}")
        print(f"Testing {symbol} {tf}")
        df = load_mt5_csv(filepath, symbol=symbol)
        print(f"  Loaded {len(df):,} bars")

        res = fast_backtest(df, symbol, tf, starting_equity=200.0, risk_per_trade=1.8, min_score=0.67, step=6)
        print(f"  Trades: {res['trades']} | Win: {res['win_rate']}% | Expectancy: {res['expectancy_r']}R | PF: {res['profit_factor']} | Final: R{res['final_equity']}")

        results.append({"symbol": symbol, "tf": tf, **res})

    print("\n\n" + "=" * 75)
    print("FINAL RESULTS SUMMARY")
    print("=" * 75)
    print(f"{'Symbol':<8} {'TF':<5} {'Trades':>7} {'Win%':>7} {'Expect(R)':>10} {'PF':>6} {'Final Eq':>10} {'Return%':>8}")
    print("-" * 75)
    for r in results:
        print(f"{r['symbol']:<8} {r['tf']:<5} {r['trades']:>7} {r['win_rate']:>6.1f}% {r['expectancy_r']:>10.3f} {r['profit_factor']:>6.2f} {r['final_equity']:>10.0f} {r['return_pct']:>7.1f}%")

    print("\nKey observations for your aggressive goal:")
    print("  - Positive expectancy + decent profit factor = green light to paper trade")
    print("  - Very low trade frequency is normal and GOOD for structure systems")
    print("  - Focus more on expectancy and profit factor than raw win rate")


if __name__ == "__main__":
    main()
