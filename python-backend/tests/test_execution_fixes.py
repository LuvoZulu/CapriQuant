"""Regression tests for execution + signal path fixes."""

from datetime import datetime

from app.live_data import to_naive_utc
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