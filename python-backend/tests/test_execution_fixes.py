"""Regression tests for execution + signal path fixes."""

from datetime import datetime, timedelta

import pandas as pd

from app.live_data import (
    add_market_data,
    catchup_cutoff,
    clear_buffer,
    filter_df_to_catchup_window,
    get_recent_df,
    is_within_catchup_window,
    to_naive_utc,
)
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


def test_backfill_upserts_older_bars_without_time_travel():
    clear_buffer("TEST")
    now = datetime.utcnow().replace(second=0, microsecond=0)

    add_market_data("TEST", {
        "timestamp": now,
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100.5,
        "volume": 10,
    })
    add_market_data("TEST", {
        "timestamp": now - timedelta(minutes=2),
        "open": 98,
        "high": 99,
        "low": 97,
        "close": 98.5,
        "volume": 7,
        "backfill": True,
    })
    add_market_data("TEST", {
        "timestamp": now - timedelta(minutes=1),
        "open": 99,
        "high": 100,
        "low": 98,
        "close": 99.5,
        "volume": 8,
        "backfill": True,
    })

    df = get_recent_df("TEST")
    assert list(df["timestamp"]) == sorted(df["timestamp"])
    assert list(df["close"]) == [98.5, 99.5, 100.5]


def test_duplicate_live_minute_does_not_add_cumulative_volume():
    clear_buffer("VOLTEST")
    now = datetime.utcnow().replace(second=0, microsecond=0)

    add_market_data("VOLTEST", {
        "timestamp": now,
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100.5,
        "volume": 10,
    })
    add_market_data("VOLTEST", {
        "timestamp": now,
        "open": 100,
        "high": 102,
        "low": 98,
        "close": 101.5,
        "volume": 12,
    })

    df = get_recent_df("VOLTEST")
    assert len(df) == 1
    assert df.iloc[0]["volume"] == 12
    assert df.iloc[0]["high"] == 102
    assert df.iloc[0]["low"] == 98
