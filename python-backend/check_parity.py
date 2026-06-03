"""
Live vs Backtest Parity Checker (Phase 2)

Run after having market_data populated:
  cd python-backend
  python check_parity.py --symbol XAUUSD --bars 500

It will:
- Load recent bars from DB for the symbol (M5 or M1)
- Run the current production signal path (get_structure_signal or MTF)
- Compare to what a backtest would have produced on the same window (simplified)
- Report divergences (useful for detecting if live path drifted from backtest assumptions)

This helps ensure the "honest backtest" matches live decisions.
"""

import argparse
import pandas as pd
from datetime import datetime
import sys
sys.path.insert(0, '.')

from app.db import get_conn_cursor
from app.features.builder import compute_structure
from app.engine.confluence import get_structure_signal
from app.engine.multi_timeframe import get_mtf_structure_signal
from app.backtest.replay import run_backtest
from app.config import get_settings

def load_recent_bars(symbol: str, timeframe: str = "M5", limit: int = 500) -> pd.DataFrame:
    conn, cur = get_conn_cursor()
    try:
        cur.execute("""
            SELECT timestamp, open, high, low, close, tick_volume as volume
            FROM market_data
            WHERE symbol = %s AND timeframe = %s
            ORDER BY timestamp DESC
            LIMIT %s
        """, (symbol, timeframe, limit))
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.iloc[::-1].reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    finally:
        cur.close()
        # pool will handle

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--bars", type=int, default=300)
    parser.add_argument("--tf", default="M5")
    args = parser.parse_args()

    s = get_settings()
    print(f"Checking parity for {args.symbol} last {args.bars} {args.tf} bars...")

    df = load_recent_bars(args.symbol, args.tf, args.bars)
    if len(df) < 50:
        print("Not enough data in DB. Run the data feeder EA first.")
        return

    # Current live-style signal on the end window
    window = df.tail(200).copy()
    ms = compute_structure(window, symbol=args.symbol, timeframe=args.tf, min_candles=10)
    live_sig = get_structure_signal(ms)
    mtf_sig = get_mtf_structure_signal(args.symbol, spread=0)  # if buffers allow, else None

    print("\n=== Current live-style signal (end of window) ===")
    print(live_sig.get("signal"), "setup:", live_sig.get("setup"), "conf:", live_sig.get("confidence"))

    if mtf_sig:
        print("MTF signal:", mtf_sig.get("signal"), mtf_sig.get("engine"))

    # Run honest backtest on the data (will use full path + costs + RM)
    print("\n=== Running honest backtest on same data for comparison ===")
    bt_res = run_backtest(df, symbol=args.symbol, timeframe=args.tf, step=10, spread_points=s.default_spread_points, use_risk_manager=True)
    bt_trades = bt_res.get("trades", [])
    print(f"Backtest trades in window: {len(bt_trades)}")
    if bt_trades:
        last = bt_trades[-1]
        print("Last backtest trade:", last.get("direction"), "R=", last.get("r_multiple"), "exit_reason=", last.get("exit_reason"))

    print("\nParity note: Compare the live signal above to what backtest would have decided on identical recent bars.")
    print("Divergences may indicate forming-bar pollution, param drift, or costs not accounted in live sizing.")

if __name__ == "__main__":
    main()
