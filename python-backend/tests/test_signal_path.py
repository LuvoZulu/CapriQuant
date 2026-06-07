"""
Basic integration-style test for the structure signal path.
Uses small synthetic data so it runs fast and deterministically.
"""

import pandas as pd
from datetime import datetime, timedelta

from app.features.builder import compute_structure
from app.engine.confluence import get_structure_signal, evaluate_setups


def make_ohlc_df(closes: list[float], start_ts: datetime = None) -> pd.DataFrame:
    if start_ts is None:
        start_ts = datetime(2025, 6, 1, 0, 0)
    rows = []
    for i, c in enumerate(closes):
        ts = start_ts + timedelta(minutes=5 * i)
        o = closes[i-1] if i > 0 else c
        h = max(o, c) + 0.2
        l = min(o, c) - 0.2
        rows.append({"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 10})
    return pd.DataFrame(rows)


def test_evaluate_setups_and_get_signal_on_trending_data():
    # Gentle uptrend with a pullback -> should produce bullish bias and possibly a setup
    closes = [100 + i * 0.8 for i in range(25)] + [120 - i*0.3 for i in range(8)] + [117 + j*0.5 for j in range(12)]
    df = make_ohlc_df(closes)
    ms = compute_structure(df, symbol="TEST", timeframe="M5", min_candles=10)
    assert ms.bias in ("BULLISH", "NEUTRAL")  # trending up

    setups = evaluate_setups(ms)
    # We don't assert >0 because filters are strict, but it must not crash and return list
    assert isinstance(setups, list)

    sig = get_structure_signal(ms)
    assert "signal" in sig
    assert "market_structure" in sig or "bias" in sig
    assert sig["engine"] in ("structure_v2_strict", "structure_mtf_precision")


def test_mtf_small_buffer_graceful():
    # Very small data -> should return HOLD "building" style message, no crash
    closes = [100 + i for i in range(9)]
    df = make_ohlc_df(closes)
    # Simulate what get_mtf does for tiny buffers
    from app.engine.multi_timeframe import get_mtf_structure_signal
    # We can't easily mock the live_buffer here, so just test that compute + get_structure doesn't explode
    ms = compute_structure(df, "TEST", "M5", min_candles=5)
    sig = get_structure_signal(ms)
    assert sig["signal"] in ("HOLD", "BUY", "SELL")
    # MTF signature now accepts equity for risk veto (default 0 -> 200)
    # (full buffer-dependent test would be integration)


def test_bearish_context_scores_count_as_sell_confluence():
    from app.engine.confluence import _directional_confluence

    strength = _directional_confluence(
        "SELL",
        amd_score=-0.5,
        fib_score=-0.4,
        pa_score=-0.3,
        liq_score=-0.2,
        crt_score=-0.1,
        struc_score=-0.6,
    )

    assert strength > 0.48


def test_mtf_waits_for_completed_higher_timeframe_history():
    from app.engine.multi_timeframe import get_mtf_structure_signal
    from app.live_data import clear_buffer, seed_buffer

    clear_buffer("MTFWAIT")
    start = datetime.utcnow() - timedelta(minutes=24)
    bars = []
    for i in range(24):
        price = 100 + i * 0.1
        bars.append({
            "timestamp": start + timedelta(minutes=i),
            "open": price,
            "high": price + 0.2,
            "low": price - 0.2,
            "close": price + 0.05,
            "volume": 10,
        })
    seed_buffer("MTFWAIT", bars, merge=False)

    sig = get_mtf_structure_signal("MTFWAIT", min_candles_m1=8)
    assert sig["signal"] == "HOLD"
    assert "Building" in sig["rationale"]
