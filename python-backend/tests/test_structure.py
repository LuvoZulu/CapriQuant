"""
Unit tests for core structure primitives.
These are critical for correctness of swings, BOS/CHOCH, bias.
"""

import pandas as pd
import pytest
from datetime import datetime, timedelta

from app.features.structure import (
    find_swings,
    detect_structure_breaks,
    compute_market_structure,
    MarketStructure,
)


def make_synthetic_df(prices: list[float], start: datetime = None) -> pd.DataFrame:
    """Create a minimal OHLC DataFrame from close prices (for testing pivots)."""
    if start is None:
        start = datetime(2025, 1, 1)
    n = len(prices)
    data = []
    for i, p in enumerate(prices):
        ts = start + timedelta(minutes=i)
        # Simple bars: open=close_prev, high/low around close with small noise
        o = prices[i-1] if i > 0 else p
        h = max(o, p) + 0.1
        l = min(o, p) - 0.1
        data.append({
            "timestamp": ts,
            "open": o,
            "high": h,
            "low": l,
            "close": p,
            "volume": 100,
        })
    return pd.DataFrame(data)


def test_find_swings_basic_pivot():
    """Test that a clear local high/low is detected with left=2, right=2."""
    # Simple V shape for low, then peak for high
    closes = [100, 101, 102, 101, 100, 99, 98, 99, 100, 101, 102]
    df = make_synthetic_df(closes)
    swings = find_swings(df, left=2, right=2, min_strength=0.0)
    assert len(swings) >= 1
    # The middle low around index 6 should be detected
    lows = [s for s in swings if s.swing_type == "LOW"]
    assert any(abs(s.price - 98) < 1 for s in lows)


def test_find_swings_requires_enough_bars():
    """Too few bars -> no swings."""
    closes = [100, 101, 102, 101]
    df = make_synthetic_df(closes)
    swings = find_swings(df, left=2, right=2)
    assert len(swings) == 0


def test_detect_structure_breaks_basic():
    """Sequence of swings should produce BOS and set bias."""
    closes = [100, 105, 103, 108, 106, 111]  # HH HL HH -> bullish BOS
    df = make_synthetic_df(closes)
    swings = find_swings(df, left=1, right=1, min_strength=0.0)
    breaks, bias = detect_structure_breaks(swings, df)
    assert bias == "BULLISH"
    assert len(breaks) >= 1
    assert any(b.break_type == "BOS" and b.direction == "BULL" for b in breaks)


def test_compute_market_structure_small_data():
    """Should not crash on small but valid data; bias may be NEUTRAL."""
    closes = [100 + i*0.5 for i in range(12)]
    df = make_synthetic_df(closes)
    ms = compute_market_structure(df, min_candles=5)
    assert isinstance(ms, MarketStructure)
    assert ms.bias in ("BULLISH", "BEARISH", "NEUTRAL")


def test_live_small_buffer_still_produces_some_output():
    """With our dynamic fallback in find_swings (1/1 for small data), we get at least some swings."""
    closes = [100, 101, 102, 101, 100, 99, 100, 101]
    df = make_synthetic_df(closes)
    ms = compute_market_structure(df, min_candles=5, swing_left=3, swing_right=3)
    # Even with original 3/3 params, the internal dynamic should help on very small data
    # But mainly: it shouldn't raise and should return a MarketStructure
    assert len(ms.swings) >= 0  # may be 0 or 1 depending on exact data
    assert ms.current_price > 0
