"""
Live Data Buffer for real-time structure analysis.
Maintains recent bars in memory per symbol so we can run the structure engine
on fresh data instead of waiting for DB.
"""

from collections import deque
from typing import Dict, List, Optional, Deque
import pandas as pd
from datetime import datetime

# Store last N bars per symbol (normalized symbol)
# Each bar: {'timestamp': datetime, 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float}
LIVE_BUFFERS: Dict[str, Deque[dict]] = {}

# How many bars to keep in memory per symbol (enough for swing detection + structure)
MAX_BARS_PER_SYMBOL = 300


def update_live_bar(symbol: str, bar: dict):
    """
    Update the live buffer with a new bar or tick.
    If it's a TICK, we update the current forming bar.
    Expects bar to have at least 'close'. Optional: open, high, low, volume, timestamp.
    """
    symbol = symbol.upper()

    if symbol not in LIVE_BUFFERS:
        LIVE_BUFFERS[symbol] = deque(maxlen=MAX_BARS_PER_SYMBOL)

    buffer = LIVE_BUFFERS[symbol]

    ts = bar.get("timestamp")
    if ts is None:
        ts = datetime.utcnow()
    elif isinstance(ts, str):
        try:
            ts = pd.to_datetime(ts)
        except:
            ts = datetime.utcnow()

    close = float(bar.get("close", 0))
    open_ = float(bar.get("open", close))
    high = float(bar.get("high", close))
    low = float(bar.get("low", close))
    volume = float(bar.get("volume", 0))

    new_bar = {
        "timestamp": ts,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }

    if buffer and buffer[-1]["timestamp"] == new_bar["timestamp"]:
        # Update current bar (typical for tick updates on same bar)
        buffer[-1] = new_bar
    else:
        buffer.append(new_bar)


def get_recent_df(symbol: str, min_bars: int = 30) -> Optional[pd.DataFrame]:
    """
    Return a DataFrame of recent bars for the symbol, ready for compute_market_structure.
    Returns None if not enough data.
    """
    symbol = symbol.upper()
    if symbol not in LIVE_BUFFERS:
        return None

    buffer = LIVE_BUFFERS[symbol]
    if len(buffer) < min_bars:
        return None

    df = pd.DataFrame(list(buffer))
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def get_latest_price(symbol: str) -> Optional[dict]:
    """Quick access to the most recent price info."""
    symbol = symbol.upper()
    if symbol not in LIVE_BUFFERS or not LIVE_BUFFERS[symbol]:
        return None
    last = LIVE_BUFFERS[symbol][-1]
    return {
        "timestamp": last["timestamp"],
        "close": last["close"],
        "high": last["high"],
        "low": last["low"],
    }


def clear_buffer(symbol: str = None):
    """Mainly for testing/debugging."""
    if symbol:
        LIVE_BUFFERS.pop(symbol.upper(), None)
    else:
        LIVE_BUFFERS.clear()
