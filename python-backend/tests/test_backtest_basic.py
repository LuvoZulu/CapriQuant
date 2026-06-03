"""
Smoke + determinism checks for the backtest harness.
"""

import pandas as pd
from datetime import datetime, timedelta
import pytest

from app.backtest.replay import run_backtest


def make_test_df(n: int = 300) -> pd.DataFrame:
    start = datetime(2025, 1, 1)
    rows = []
    price = 100.0
    for i in range(n):
        ts = start + timedelta(minutes=5 * i)
        o = price
        c = price + (0.5 if i % 3 == 0 else -0.2)
        h = max(o, c) + 0.3
        l = min(o, c) - 0.3
        price = c
        rows.append({
            "timestamp": ts,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": 100,
        })
    return pd.DataFrame(rows)


def test_backtest_runs_and_produces_output():
    df = make_test_df(250)
    res = run_backtest(df, symbol="TEST", timeframe="M5", risk_per_trade=1.0, step=10)
    assert "summary" in res
    assert "trades" in res
    assert isinstance(res["trades"], list)
    if res["trades"]:
        t = res["trades"][0]
        assert "r_multiple" in t
        assert "direction" in t


def test_backtest_is_reasonably_deterministic():
    """Same data + params should give same number of trades (ignoring tiny float diffs)."""
    df = make_test_df(200)
    r1 = run_backtest(df, symbol="TEST", timeframe="M5", risk_per_trade=1.0, step=8, min_confluence_score=0.5)
    r2 = run_backtest(df, symbol="TEST", timeframe="M5", risk_per_trade=1.0, step=8, min_confluence_score=0.5)
    assert len(r1["trades"]) == len(r2["trades"])
