"""
Super quick test - only XAUUSD M5 with larger steps.
Run this first to get an early signal of edge.
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent))

from utils.load_mt5_data import load_mt5_csv
from app.features.structure import compute_market_structure
from app.engine.confluence import evaluate_setups

def quick_test():
    df = load_mt5_csv("../testing/XAUUSDm_M5_202501012305_202605292055.csv", symbol="XAUUSD")
    print(f"XAUUSD M5: {len(df):,} bars loaded")

    trades = []
    equity = 200.0
    window = 160
    step = 12   # bigger step = faster

    for i in range(window, len(df) - 40, step):
        wdf = df.iloc[i-window:i]
        try:
            ms = compute_market_structure(wdf, "XAUUSD", "M5")
            setups = evaluate_setups(ms)
            if not setups: continue
            best = setups[0]
            if best.score < 0.74: continue  # Stricter after v2 update

            entry = float(df.iloc[i]["open"])
            stop = best.stop_suggestion
            tp1 = best.tp1

            # Quick simulation
            fut = df.iloc[i+1:i+40]
            for _, b in fut.iterrows():
                if best.direction == "BUY":
                    if b.low <= stop: 
                        r = (stop - entry) / (entry - stop)   # negative
                        break
                    if b.high >= tp1:
                        r = (tp1 - entry) / (entry - stop)
                        break
                else:
                    if b.high >= stop:
                        r = (entry - stop) / (entry - stop) * -1
                        break
                    if b.low <= tp1:
                        r = (entry - tp1) / (entry - stop)
                        break
            else:
                continue

            pnl = equity * 0.018 * r
            equity += pnl
            trades.append(r)

        except:
            continue

    if trades:
        wins = [x for x in trades if x > 0]
        print(f"\nTrades taken: {len(trades)}")
        print(f"Win rate: {len(wins)/len(trades)*100:.1f}%")
        print(f"Expectancy (R): {sum(trades)/len(trades):.3f}")
        print(f"Final equity: R{equity:.0f}")
    else:
        print("No qualifying trades in this pass (normal for strict structure rules)")

if __name__ == "__main__":
    quick_test()
