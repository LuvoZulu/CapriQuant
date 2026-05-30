"""
Quick local test script for the new CapriQuant structure engine.

Run this after you have some data in the database, or feed a DataFrame directly.

This proves the full new stack (structure → contextual strategies → confluence) is working.
"""

import pandas as pd
from app.features.structure import compute_market_structure
from app.engine.confluence import get_structure_signal


def test_with_dataframe(df: pd.DataFrame, symbol="XAUUSD", tf="M5"):
    print(f"\n=== Testing Structure Engine on {symbol} {tf} ===")
    ms = compute_market_structure(df, symbol=symbol, timeframe=tf)

    print("Market Structure Summary:")
    print(ms.to_dict())

    signal = get_structure_signal(ms)
    print("\n=== FINAL SIGNAL ===")
    print(signal)
    return signal


if __name__ == "__main__":
    print("CapriQuant Structure Engine Test")
    print("Load real data and call test_with_dataframe(your_df)")
    print("Or use the backtest harness in app/backtest/replay.py")
