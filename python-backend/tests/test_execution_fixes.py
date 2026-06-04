"""Regression tests for execution + signal path fixes."""

from datetime import datetime, timedelta

import pandas as pd

from app.live_data import to_naive_utc, filter_df_to_catchup_window, is_within_catchup_window, catchup_cutoff
from app.api.signals import _resolve_validated_stop


def test_mt5_timestamp_parsing():
    ts = to_naive_utc("2026.06.04 14:30:00")
    assert ts == datetime(2026, 6, 4, 14, 30, 0)


def test_validated_stop_never_uses_current_price():
    sig = {
        "signal": "BUY",
        "stop_suggestion": 2650.5,
        "market_structure": {"current_price": 2660.0, "symbol": "XAUUSD"},
    }
    assert _resolve_validated_stop(sig) == 2650.5

    sig2 = {
        "signal": "BUY",
        "market_structure": {"current_price": 2660.0},
    }
    assert _resolve_validated_stop(sig2) is None


def test_catchup_window_filters_old_bars():
    now = datetime.utcnow()
    old = now - timedelta(hours=48)
    df = pd.DataFrame([
        {"timestamp": old, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1},
        {"timestamp": now - timedelta(hours=1), "open": 2, "high": 3, "low": 1.5, "close": 2.5, "volume": 1},
    ])
    out = filter_df_to_catchup_window(df)
    assert len(out) == 1
    assert is_within_catchup_window(now - timedelta(hours=1))
    assert not is_within_catchup_window(old)
    assert catchup_cutoff() <= now - timedelta(hours=23, minutes=59)