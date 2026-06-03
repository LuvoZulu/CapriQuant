"""
Real-time Live Data Buffer with proper M1 bar aggregation.

This version aggregates incoming TICK data into clean M1 bars so the
structure engine can properly detect swings, Order Blocks, BOS/CHOCH, etc.
from recent live market movement instead of stale DB data.
"""

from collections import deque
from typing import Dict, Optional, Deque
import pandas as pd
from datetime import datetime, timezone

# Per-symbol buffer of completed M1 bars + the current forming bar
# Each entry: {'timestamp': datetime, 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float}
LIVE_BARS: Dict[str, Deque[dict]] = {}

# How many completed M1 bars to keep per symbol (enough for good swing detection)
MAX_COMPLETED_BARS = 700


def _floor_to_minute(ts: datetime) -> datetime:
    """Floor a timestamp to the start of its minute."""
    return ts.replace(second=0, microsecond=0)


def update_live_bar(symbol: str, bar: dict):
    """
    Feed new market data (from TICK or bar updates) into the live buffer.

    This function aggregates data into proper M1 bars.
    Call this on every incoming payload from the EA.
    """
    symbol = symbol.upper()

    if symbol not in LIVE_BARS:
        LIVE_BARS[symbol] = deque(maxlen=MAX_COMPLETED_BARS + 1)

    buffer = LIVE_BARS[symbol]

    # Determine the timestamp of this update
    ts_raw = bar.get("timestamp")
    if ts_raw is None:
        ts = datetime.now(timezone.utc)
    elif isinstance(ts_raw, str):
        try:
            ts = pd.to_datetime(ts_raw).to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except:
            ts = datetime.now(timezone.utc)
    else:
        ts = ts_raw

    close = float(bar.get("close", 0))
    open_ = float(bar.get("open", close))
    high = float(bar.get("high", close))
    low = float(bar.get("low", close))
    volume = float(bar.get("volume", 0))

    bar_minute = _floor_to_minute(ts)

    if not buffer:
        # First ever bar for this symbol
        buffer.append({
            "timestamp": bar_minute,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })
        return

    last_bar = buffer[-1]

    if last_bar["timestamp"] == bar_minute:
        # Still inside the same minute → update the current forming bar
        last_bar["high"] = max(last_bar["high"], high)
        last_bar["low"] = min(last_bar["low"], low)
        last_bar["close"] = close
        last_bar["volume"] += volume
    else:
        # New minute started → the previous bar is now complete.
        # Append the new forming bar.
        buffer.append({
            "timestamp": bar_minute,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })


def get_recent_df(symbol: str, min_bars: int = 15) -> Optional[pd.DataFrame]:
    """
    Returns a DataFrame of recent M1 bars (completed + current forming)
    ready to be fed into compute_market_structure.

    This is the main function the real-time signal logic should use.
    """
    symbol = symbol.upper()
    if symbol not in LIVE_BARS:
        return None

    buffer = LIVE_BARS[symbol]
    if len(buffer) < min_bars:
        return None

    df = pd.DataFrame(list(buffer))
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def get_latest_price(symbol: str) -> Optional[dict]:
    """Returns the most recent close + timestamp from the live buffer."""
    symbol = symbol.upper()
    if symbol not in LIVE_BARS or not LIVE_BARS[symbol]:
        return None

    last = LIVE_BARS[symbol][-1]
    return {
        "timestamp": last["timestamp"],
        "close": last["close"],
        "high": last["high"],
        "low": last["low"],
    }


def get_buffer_length(symbol: str) -> int:
    """Debug helper."""
    symbol = symbol.upper()
    return len(LIVE_BARS.get(symbol, []))


def clear_buffer(symbol: str = None):
    """Mainly for debugging."""
    if symbol:
        LIVE_BARS.pop(symbol.upper(), None)
    else:
        LIVE_BARS.clear()


def get_all_buffer_lengths() -> dict:
    """Returns how many bars are currently stored in the live buffer for each symbol."""
    return {sym: len(buf) for sym, buf in LIVE_BARS.items()}


def get_buffer_info(symbol: str) -> dict:
    """Returns detailed info about the live buffer for one symbol."""
    symbol = symbol.upper()
    if symbol not in LIVE_BARS or not LIVE_BARS[symbol]:
        return {"symbol": symbol, "count": 0, "oldest": None, "newest": None}

    buf = LIVE_BARS[symbol]
    return {
        "symbol": symbol,
        "count": len(buf),
        "oldest": buf[0]["timestamp"].isoformat() if buf else None,
        "newest": buf[-1]["timestamp"].isoformat() if buf else None,
        "latest_close": buf[-1]["close"] if buf else None,
    }


def get_recent_closed_df(symbol: str, limit=None):
    """
    Returns recent M1 bars excluding the current forming (last) minute.
    Strongly recommended for live structure calls (compute_structure, MTF) to avoid spurious swings, BOS/CHOCH, OBs, FVGs and CRT from the still-updating bar.
    """
    df = get_recent_df(symbol, limit)
    if df is None or len(df) < 2:
        return df
    return df.iloc[:-1].reset_index(drop=True)
