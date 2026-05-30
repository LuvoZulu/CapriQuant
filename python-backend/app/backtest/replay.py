"""
CapriQuant Backtesting / Replay Harness (Skeleton)

This module lets you replay historical data bar-by-bar through the new
structure + confluence engine and measure real expectancy.

HOW TO USE (once you have data):
1. Export historical M1/M5/M15 bars for your symbols (XAUUSD, NAS100, etc.)
2. Load into pandas DataFrame with columns: timestamp, open, high, low, close, volume
3. Call run_backtest(df, symbol, timeframe)
4. Review the returned trades + statistics

This is deliberately simple and pure so you can trust the results.
"""

import pandas as pd
from typing import List, Dict
from datetime import datetime

from app.features.structure import compute_market_structure
from app.engine.confluence import evaluate_setups, Setup


def run_backtest(
    df: pd.DataFrame,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    starting_equity: float = 200.0,
    risk_per_trade: float = 1.8,
    min_confluence_score: float = 0.65,
    step: int = 5,                    # New: step every N bars for speed (use 1 for maximum accuracy)
) -> Dict:
    """
    Walk-forward style replay through the structure engine.
    Returns detailed trade log + performance summary.
    """
    df = df.sort_values("timestamp").reset_index(drop=True)
    equity = starting_equity
    trades: List[Dict] = []
    equity_curve = [equity]

    print(f"[Backtest] Starting replay of {len(df)} bars for {symbol} {timeframe} (step={step})")

    # We process in rolling windows of ~180-220 bars (mimics live 200-candle limit)
    window = 200

    for end in range(window, len(df), step):
        window_df = df.iloc[end-window:end].copy()
        ms = compute_market_structure(window_df, symbol=symbol, timeframe=timeframe)

        setups = evaluate_setups(ms)

        if not setups:
            continue

        best = setups[0]
        if best.score < min_confluence_score:
            continue

        # Simulate entry at next bar open (very conservative)
        entry_bar = df.iloc[end]
        entry_price = float(entry_bar["open"])

        # Use the engine's structural stop
        stop = best.stop_suggestion
        tp1 = best.tp1
        tp2 = best.tp2

        # Very basic outcome simulation (you can make this more sophisticated)
        future = df.iloc[end+1 : end+60]   # look up to 60 bars ahead
        hit_tp1 = False
        hit_tp2 = False
        hit_stop = False
        exit_price = entry_price
        exit_reason = "timeout"

        for _, bar in future.iterrows():
            if best.direction == "BUY":
                if bar["low"] <= stop:
                    hit_stop = True
                    exit_price = stop
                    exit_reason = "stop"
                    break
                if bar["high"] >= tp2:
                    hit_tp2 = True
                    exit_price = tp2
                    exit_reason = "tp2"
                    break
                if bar["high"] >= tp1 and not hit_tp1:
                    hit_tp1 = True
                    exit_price = tp1
                    exit_reason = "tp1"
                    break
            else:  # SELL
                if bar["high"] >= stop:
                    hit_stop = True
                    exit_price = stop
                    exit_reason = "stop"
                    break
                if bar["low"] <= tp2:
                    hit_tp2 = True
                    exit_price = tp2
                    exit_reason = "tp2"
                    break
                if bar["low"] <= tp1 and not hit_tp1:
                    hit_tp1 = True
                    exit_price = tp1
                    exit_reason = "tp1"
                    break

        # P&L calculation (very approximate - assumes 1 unit per "lot" concept)
        risk_dist = abs(entry_price - stop)
        if risk_dist == 0:
            continue

        reward = abs(exit_price - entry_price)
        r_multiple = reward / risk_dist if best.direction == "BUY" else reward / risk_dist * -1
        if best.direction == "SELL":
            r_multiple = -r_multiple if exit_reason == "stop" else r_multiple

        pnl = equity * (risk_per_trade / 100.0) * r_multiple
        equity += pnl

        trade = {
            "entry_time": entry_bar["timestamp"],
            "direction": best.direction,
            "setup": best.name,
            "entry": round(entry_price, 5),
            "stop": round(stop, 5),
            "exit": round(exit_price, 5),
            "exit_reason": exit_reason,
            "r_multiple": round(r_multiple, 2),
            "pnl": round(pnl, 2),
            "equity": round(equity, 2),
            "confluences": best.confluences,
            "rationale": best.rationale,
        }
        trades.append(trade)
        equity_curve.append(equity)

        if len(trades) % 25 == 0:
            print(f"  Processed bar {end} | Equity: {equity:.2f} | Trades: {len(trades)}")

    # Summary statistics
    if not trades:
        return {"trades": [], "summary": "No trades taken under current filters"}

    wins = [t for t in trades if t["r_multiple"] > 0]
    losses = [t for t in trades if t["r_multiple"] <= 0]

    loss_sum = abs(sum(t["r_multiple"] for t in losses))
    profit_factor = round(sum(t["r_multiple"] for t in wins) / loss_sum, 2) if loss_sum > 0 else 99.0

    summary = {
        "total_trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "profit_factor": profit_factor,
        "expectancy_r": round(sum(t["r_multiple"] for t in trades) / len(trades), 2),
        "final_equity": round(equity, 2),
        "return_pct": round((equity - starting_equity) / starting_equity * 100, 1),
        "max_equity": round(max(equity_curve), 2),
        "min_equity": round(min(equity_curve), 2),
    }

    print("\n[Backtest Complete]")
    print(summary)

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "summary": summary,
    }


if __name__ == "__main__":
    print("Backtest harness ready. Load your DataFrame and call run_backtest(df).")
