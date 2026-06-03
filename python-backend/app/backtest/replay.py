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
from app.engine.confluence import get_structure_signal
from app.risk import RiskManager, RiskParams


def run_backtest(
    df: pd.DataFrame,
    symbol: str = "XAUUSD",
    timeframe: str = "M5",
    starting_equity: float = 200.0,
    risk_per_trade: float = 1.8,  # fallback / initial; now RiskManager dynamic used
    min_confluence_score: float = 0.65,
    step: int = 5,                    # step every N bars for speed (use 1 for max accuracy)
    spread_points: float = 0.30,      # round-turn spread cost in price units (XAU ~0.2-0.6, indices vary)
    commission_r_per_trade: float = 0.02,  # extra cost in R units (commissions/swaps/slippage proxy)
    use_risk_manager: bool = True,
) -> Dict:
    """
    Honest walk-forward backtest using FULL production signal path + RiskManager + realistic costs.
    - Uses get_structure_signal (evaluate + confluence + stops from structure)
    - RiskManager for dynamic risk_pct based on simulated streak (hard circuits too)
    - Costs: spread*2 + commission_r subtracted from every r_multiple (critical for realism)
    - Entry on next bar open; SL/TP hit detection on wicks (conservative)
    - Tracks simulated streak for risk de-risking
    """
    df = df.sort_values("timestamp").reset_index(drop=True)
    equity = starting_equity
    trades: List[Dict] = []
    equity_curve = [equity]
    current_streak = 0  # simulated loss streak for RiskManager
    daily_r_this_day = 0.0  # simplistic (no date split for speed)
    day_start_equity = equity

    print(f"[Backtest HONEST] Starting replay of {len(df)} bars for {symbol} {timeframe} (step={step}, costs spread={spread_points})")

    # Risk manager instance (real one, non-bypassable in sim too)
    rm = None
    if use_risk_manager:
        params = RiskParams(
            account_equity=equity,
            starting_equity=starting_equity,
            target_equity=17000.0,
            max_daily_loss_pct=6.0,
        )
        rm = RiskManager(params)

    # We process in rolling windows of ~180-220 bars (mimics live 200-candle limit)
    window = 200

    for end in range(window, len(df), step):
        window_df = df.iloc[end-window:end].copy()
        ms = compute_market_structure(window_df, symbol=symbol, timeframe=timeframe)

        # FULL production signal (was: raw evaluate_setups + manual score filter)
        sig = get_structure_signal(ms, spread=spread_points)

        direction = sig.get("signal")
        if direction not in ("BUY", "SELL"):
            continue

        # Apply the REAL RiskManager veto + dynamic sizing inside backtest (was fixed risk_per_trade)
        eff_risk_pct = risk_per_trade
        if rm is not None:
            # update rm equity snapshot
            rm.p.account_equity = equity
            allowed, veto, dyn_risk = rm.can_take_trade(
                recent_loss_streak=current_streak,
                today_pnl= (daily_r_this_day * (equity * 0.015) ),  # proxy
                starting_equity_today=day_start_equity,
            )
            if not allowed:
                continue  # hard veto in sim too -> honest
            eff_risk_pct = dyn_risk if dyn_risk > 0.1 else risk_per_trade

        # entry at NEXT bar open (production conservative)
        if end + 1 >= len(df):
            continue
        entry_bar = df.iloc[end + 1]  # next bar
        entry_price = float(entry_bar["open"])

        # structural levels from the production signal
        stop = float(sig.get("stop_suggestion") or sig.get("stop") or (entry_price - 0.5))
        tp1 = float(sig.get("tp1") or entry_price + (abs(entry_price - stop) * 1.8))
        tp2 = float(sig.get("tp2") or entry_price + (abs(entry_price - stop) * 3.0))

        if abs(entry_price - stop) < 1e-9:
            continue

        # Simulate future for SL/TP/timeout (wick hits)
        future = df.iloc[end + 2 : end + 2 + 120]  # more look ahead for realism
        hit_stop = False
        hit_tp2 = False
        hit_tp1 = False
        exit_price = entry_price
        exit_reason = "timeout"

        for _, bar in future.iterrows():
            if direction == "BUY":
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
            else:
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

        # === HONEST R with costs ===
        risk_dist = abs(entry_price - stop)
        if risk_dist < 1e-9:
            continue

        # signed price pnl
        if direction == "BUY":
            pnl_price = exit_price - entry_price
        else:
            pnl_price = entry_price - exit_price

        r_gross = pnl_price / risk_dist

        # realistic costs in R units (spread roundtrip + commission proxy)
        cost_r = (spread_points * 2.0 / risk_dist) + commission_r_per_trade
        r_multiple = r_gross - cost_r

        # size using the (dynamic) risk we decided
        pnl = equity * (eff_risk_pct / 100.0) * r_multiple
        equity += pnl

        # update sim streak + daily proxy for next risk decisions
        if r_multiple < 0:
            current_streak += 1
        else:
            current_streak = 0
        daily_r_this_day += r_multiple

        trade = {
            "entry_time": entry_bar["timestamp"],
            "direction": direction,
            "setup": sig.get("setup"),
            "entry": round(entry_price, 5),
            "stop": round(stop, 5),
            "exit": round(exit_price, 5),
            "exit_reason": exit_reason,
            "r_multiple": round(r_multiple, 3),
            "r_gross": round(r_gross, 3),
            "cost_r": round(cost_r, 3),
            "risk_pct_used": round(eff_risk_pct, 2),
            "pnl": round(pnl, 2),
            "equity": round(equity, 2),
            "confluences": sig.get("confluences", []),
            "rationale": sig.get("rationale", ""),
            "vetoed_by_risk": False,
        }
        trades.append(trade)
        equity_curve.append(equity)

        if len(trades) % 20 == 0:
            print(f"  Processed bar {end} | Equity: {equity:.2f} | Trades: {len(trades)} | streak={current_streak}")

    # Summary statistics (honest, with costs already in r)
    if not trades:
        return {"trades": [], "summary": "No trades taken under current filters (after RiskManager + confluence + costs)"}

    wins = [t for t in trades if t["r_multiple"] > 0]
    losses = [t for t in trades if t["r_multiple"] <= 0]

    loss_sum = abs(sum(t["r_multiple"] for t in losses)) or 1e-9
    pf = sum(t["r_multiple"] for t in wins) / loss_sum
    profit_factor = round(pf, 2) if loss_sum > 0 else 99.0

    total_r = sum(t["r_multiple"] for t in trades)
    expectancy = total_r / len(trades)

    # crude max dd from equity curve
    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    summary = {
        "total_trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "profit_factor": profit_factor,
        "expectancy_r": round(expectancy, 3),
        "final_equity": round(equity, 2),
        "return_pct": round((equity - starting_equity) / starting_equity * 100, 1),
        "max_equity": round(max(equity_curve), 2),
        "min_equity": round(min(equity_curve), 2),
        "max_drawdown_pct": round(max_dd, 1),
        "costs_applied": True,
        "spread_points": spread_points,
        "commission_r_per": commission_r_per_trade,
        "used_risk_manager": use_risk_manager,
        "avg_risk_pct": round(sum(t.get("risk_pct_used", risk_per_trade) for t in trades) / len(trades), 2) if trades else risk_per_trade,
    }

    print("\n[Backtest HONEST Complete]")
    print(summary)

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "summary": summary,
    }


if __name__ == "__main__":
    print("Backtest harness ready. Load your DataFrame and call run_backtest(df).")
