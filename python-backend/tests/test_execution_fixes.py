"""Regression tests for execution + signal path fixes."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

from app.live_data import (
    TICK_STATS,
    add_market_data,
    clear_buffer,
    get_buffer_length,
    get_ea_config,
    get_recent_df,
    to_naive_utc,
    update_ea_config,
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


def test_ea_config_from_payload():
    clear_buffer("CFG")
    update_ea_config("CFG", {"buffer_max_m1": 5000, "min_candles_m1": 12})
    cfg = get_ea_config("CFG")
    assert cfg["buffer_max_m1"] == 5000
    assert cfg["min_candles_m1"] == 12


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


def test_frozen_broker_timestamp_advances_on_utc():
    clear_buffer("FROZEN")
    frozen_broker = datetime(2026, 6, 8, 10, 0, 0)
    start_utc = datetime(2026, 6, 8, 12, 0, 0)

    for minute in range(20):
        utc_minute = start_utc + timedelta(minutes=minute)
        with patch("app.live_data._utc_now_minute", return_value=utc_minute):
            add_market_data("FROZEN", {
                "timestamp": frozen_broker,
                "open": 100 + minute * 0.01,
                "high": 101 + minute * 0.01,
                "low": 99 + minute * 0.01,
                "close": 100.5 + minute * 0.01,
                "volume": 10 + minute,
            })

    assert get_buffer_length("FROZEN") == 20
    assert TICK_STATS.get("FROZEN", 0) == 20


def test_broker_minute_advance_appends_normally():
    clear_buffer("LIVE")
    start = datetime(2026, 6, 8, 14, 0, 0)

    for minute in range(10):
        ts = start + timedelta(minutes=minute)
        add_market_data("LIVE", {
            "timestamp": ts,
            "open": 100 + minute,
            "high": 101 + minute,
            "low": 99 + minute,
            "close": 100.5 + minute,
            "volume": 10 + minute,
        })

    assert get_buffer_length("LIVE") == 10