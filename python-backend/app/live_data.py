"""
Live Data Buffer for real-time TICK to M1 aggregation (simpler updating logic).

Supports the real-time POST /market-data path.
Buffer size 10080 M1 bars.
The last entry in the buffer for a symbol is always the current forming minute and gets updated live on every tick.
This makes the buffer (and debug/UI) show data immediately and the numbers update on every incoming payload.
"""

from collections import deque
from typing import Dict, Optional, Deque, Any
import pandas as pd
from datetime import datetime, timezone

# Per-symbol buffer of M1 bars (last one is the current forming minute, updated live)
LIVE_BARS: Dict[str, Deque[dict]] = {}

# Real per-symbol tick counters (for debug visibility)
TICK_STATS: Dict[str, int] = {}

# 10080 M1 bars (~1 week)
MAX_COMPLETED_BARS = 10080


def _floor_to_minute(ts: datetime) -> datetime:
    """Floor a timestamp to the start of its minute."""
    return ts.replace(second=0, microsecond=0)


def add_market_data(symbol: str, data: dict) -> None:
    """
    Feed new market data (from TICK payloads) into the live buffer.
    This function aggregates into per-minute bars.
    The last bar for the symbol is the current minute and is updated in place on every call within the minute.
    Call this on every incoming payload from the EA.
    """
    symbol = symbol.upper()

    if symbol not in LIVE_BARS:
        LIVE_BARS[symbol] = deque(maxlen=MAX_COMPLETED_BARS + 1)

    TICK_STATS[symbol] = TICK_STATS.get(symbol, 0) + 1

    buffer = LIVE_BARS[symbol]

    # Determine the timestamp of this update
    ts_raw = data.get("timestamp")
    if ts_raw is None:
        ts = datetime.now(timezone.utc)
    elif isinstance(ts_raw, str):
        try:
            ts = pd.to_datetime(ts_raw).to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            ts = datetime.now(timezone.utc)
    else:
        ts = ts_raw

    # Use the M1 bar data sent by the EA (open/high/low/close/volume for current minute)
    # or fall back to the tick price
    close = float(data.get("close", 0))
    open_ = float(data.get("open", close))
    high = float(data.get("high", close))
    low = float(data.get("low", close))
    volume = float(data.get("volume", 0))

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
        # Still inside the same minute → update the current forming bar live
        last_bar["high"] = max(last_bar["high"], high)
        last_bar["low"] = min(last_bar["low"], low)
        last_bar["close"] = close
        last_bar["volume"] += volume
    else:
        # New minute started → append a new bar entry for the new minute (will be updated live)
        buffer.append({
            "timestamp": bar_minute,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })


def get_recent_df(symbol: str, limit: Optional[int] = None) -> Optional[pd.DataFrame]:
    """
    Returns a DataFrame of recent M1 bars (including the current forming minute as the last row).
    Ready to be fed into compute_structure.
    """
    symbol = symbol.upper()
    if symbol not in LIVE_BARS:
        return None

    buffer = LIVE_BARS[symbol]
    if len(buffer) < 1:
        return None

    bars = list(buffer)
    if limit:
        bars = bars[-limit:]

    df = pd.DataFrame(bars)
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def get_latest_price(symbol: str) -> Optional[dict]:
    """Returns the most recent close + timestamp from the live buffer (the current forming bar)."""
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
    """Debug helper: number of bars (minutes) seen for the symbol."""
    symbol = symbol.upper()
    return len(LIVE_BARS.get(symbol, []))


def get_buffer_status(symbol: str) -> Dict[str, Any]:
    """Debug / UI helper."""
    symbol = symbol.upper()
    buf = LIVE_BARS.get(symbol, deque())
    count = len(buf)
    last = buf[-1] if buf else None
    return {
        "symbol": symbol,
        "bars_in_buffer": count,
        "effective_bars": count,
        "max_bars": MAX_COMPLETED_BARS,
        "pct_full": round(count / MAX_COMPLETED_BARS * 100, 2) if MAX_COMPLETED_BARS > 0 else 0,
        "ticks_received": TICK_STATS.get(symbol, 0),
        "bars_completed": max(0, count - 1),
        "forming_bar": last,
    }


def get_all_buffer_lengths() -> dict:
    """Returns how many bars are currently stored for each symbol."""
    return {sym: len(buf) for sym, buf in LIVE_BARS.items()}


def clear_buffer(symbol: str = None):
    """Mainly for debugging."""
    if symbol:
        LIVE_BARS.pop(symbol.upper(), None)
    else:
        LIVE_BARS.clear()


# For compatibility with code that expects a singleton object with methods
class _LiveBufferCompat:
    def add_market_data(self, symbol: str, data: dict) -> None:
        add_market_data(symbol, data)

    def get_recent_df(self, symbol: str, limit: Optional[int] = None):
        return get_recent_df(symbol, limit)

    def get_recent_bars(self, symbol: str, limit: Optional[int] = None):
        df = get_recent_df(symbol, limit)
        if df is None or df.empty:
            return []
        return df.to_dict("records")

    def get_buffer_status(self, symbol: str):
        return get_buffer_status(symbol)

    def forming(self):
        return {}

    @property
    def buffers(self):
        return LIVE_BARS

    @property
    def max_bars(self):
        return MAX_COMPLETED_BARS


live_buffer = _LiveBufferCompat()
