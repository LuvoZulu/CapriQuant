"""
Real-time Live Data Buffer with proper M1 bar aggregation.

This version aggregates incoming TICK data into clean M1 bars so the
structure engine can properly detect swings, Order Blocks, BOS/CHOCH, etc.
from recent live market movement instead of stale DB data.
"""

from collections import deque
from typing import Dict, Optional, Deque, Any, List
import pandas as pd
from datetime import datetime, timezone, timedelta

from app.utils.symbols import normalize_symbol, symbol_variants
from app.config import get_settings

# Per-symbol buffer of completed M1 bars + the current forming bar
# Each entry: {'timestamp': datetime, 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float}
LIVE_BARS: Dict[str, Deque[dict]] = {}

# Real per-symbol tick counters (for debug visibility)
TICK_STATS: Dict[str, int] = {}

# Last successful ingest time per symbol (naive UTC)
LAST_TICK_AT: Dict[str, datetime] = {}

# Per-symbol config pushed by the EA on every /market-data post
SYMBOL_EA_CONFIG: Dict[str, Dict[str, Any]] = {}

_s = get_settings()
DEFAULT_MAX_M1_BARS = int(_s.default_buffer_max_m1)
DEFAULT_MIN_CANDLES_M1 = int(_s.default_min_candles_m1)

# Back-compat alias for modules that import MAX_COMPLETED_BARS
MAX_COMPLETED_BARS = DEFAULT_MAX_M1_BARS
MAX_M5_BARS = DEFAULT_MAX_M1_BARS // 5


def _default_ea_config() -> Dict[str, Any]:
    s = get_settings()
    return {
        "buffer_max_m1": int(s.default_buffer_max_m1),
        "min_candles_m1": int(s.default_min_candles_m1),
        "min_confidence": float(s.min_confidence_pct),
        "max_spread_points": float(s.max_spread_for_trade),
    }


def update_ea_config(symbol: str, data: dict) -> Dict[str, Any]:
    """Merge EA-supplied settings from /market-data into per-symbol config."""
    symbol = normalize_symbol(symbol)
    cfg = SYMBOL_EA_CONFIG.setdefault(symbol, _default_ea_config())
    fields = {
        "buffer_max_m1": int,
        "min_candles_m1": int,
        "min_confidence": float,
        "max_spread_points": float,
        "data_send_interval_ms": int,
    }
    for key, caster in fields.items():
        raw = data.get(key)
        if raw is None:
            continue
        try:
            cfg[key] = caster(raw)
        except (TypeError, ValueError):
            pass
    return cfg


def get_ea_config(symbol: str) -> Dict[str, Any]:
    return SYMBOL_EA_CONFIG.get(normalize_symbol(symbol), _default_ea_config())


def get_max_bars(symbol: str) -> int:
    return max(100, int(get_ea_config(symbol).get("buffer_max_m1", DEFAULT_MAX_M1_BARS)))


def get_min_candles_m1(symbol: str) -> int:
    return max(5, int(get_ea_config(symbol).get("min_candles_m1", DEFAULT_MIN_CANDLES_M1)))


def to_naive_utc(ts) -> datetime:
    """
    Normalize any timestamp to naive UTC (no tzinfo).
    Prevents pandas sort errors when mixing heterogeneous timestamp sources.
    """
    if ts is None:
        return datetime.utcnow()
    if isinstance(ts, str):
        ts = pd.to_datetime(ts, utc=True)
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if isinstance(ts, datetime) and ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    elif isinstance(ts, datetime):
        pass
    else:
        ts = pd.to_datetime(ts, utc=True).to_pydatetime()
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
    return ts


def _floor_to_minute(ts: datetime) -> datetime:
    """Floor a timestamp to the start of its minute."""
    ts = to_naive_utc(ts)
    return ts.replace(second=0, microsecond=0)


def update_live_bar(symbol: str, bar: dict):
    """
    Feed new market data (from TICK or bar updates) into the live buffer.

    This function aggregates data into proper M1 bars.
    Call this on every incoming payload from the EA.
    """
    symbol = normalize_symbol(symbol)

    if symbol not in LIVE_BARS:
        LIVE_BARS[symbol] = deque(maxlen=MAX_COMPLETED_BARS + 1)

    TICK_STATS[symbol] = TICK_STATS.get(symbol, 0) + 1
    LAST_TICK_AT[symbol] = datetime.utcnow()

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
        last_bar["volume"] = volume   # use latest reported volume (MT5 iVolume for forming bar is usually cumulative)
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


# Alias for compatibility with code that calls add_market_data
add_market_data = update_live_bar


def get_recent_df(symbol: str, min_bars: int = 15) -> Optional[pd.DataFrame]:
    """
    Returns a DataFrame of recent M1 bars (completed + current forming)
    ready to be fed into compute_market_structure.

    This is the main function the real-time signal logic should use.
    Accepts min_bars for compatibility.
    """
    symbol = normalize_symbol(symbol)
    if symbol not in LIVE_BARS:
        return None

    buffer = LIVE_BARS[symbol]
    if len(buffer) < min_bars:
        return None

    df = pd.DataFrame(list(buffer))
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"].apply(to_naive_utc))
    return df


def get_latest_price(symbol: str) -> Optional[dict]:
    """Returns the most recent close + timestamp from the live buffer."""
    symbol = normalize_symbol(symbol)
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
    symbol = normalize_symbol(symbol)
    return len(LIVE_BARS.get(symbol, []))


def clear_buffer(symbol: str = None):
    """Mainly for debugging."""
    if symbol:
        LIVE_BARS.pop(normalize_symbol(symbol), None)
        TICK_STATS.pop(normalize_symbol(symbol), None)
        LAST_TICK_AT.pop(normalize_symbol(symbol), None)
    else:
        LIVE_BARS.clear()
        TICK_STATS.clear()
        LAST_TICK_AT.clear()


def get_all_buffer_lengths() -> dict:
    """Returns how many bars are currently stored in the live buffer for each symbol."""
    return {sym: len(buf) for sym, buf in LIVE_BARS.items()}


def get_buffer_info(symbol: str) -> dict:
    """Returns detailed info about the live buffer for one symbol."""
    symbol = normalize_symbol(symbol)
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


def resample_ohlcv(df_m1: pd.DataFrame, minutes: int = 5) -> pd.DataFrame:
    """
    Resample M1 OHLCV buffer into M5, M15, etc. for multi-timeframe structure analysis.
    """
    if df_m1 is None or df_m1.empty:
        return pd.DataFrame()

    df = df_m1.copy()
    df["timestamp"] = pd.to_datetime(
        df["timestamp"].apply(to_naive_utc), utc=False
    )
    df = df.set_index("timestamp").sort_index()

    rule = f"{minutes}min"
    resampled = df.resample(rule, label="left", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["close"])

    resampled = resampled.reset_index()
    return resampled


def get_recent_m5_df(symbol: str, limit: Optional[int] = None) -> Optional[pd.DataFrame]:
    """M5 OHLCV derived from the live M1 buffer.
    When called for buffer status (limit=None), derive M5 from available M1 bars (even if < full MAX).
    This fixes dashboard showing insufficient when e.g. 37 M1 bars are present.
    """
    # For status/UI reporting, use small min_bars so we get m5 count from partial live buffer
    if limit is None:
        m1_min = 5
        df_m1 = get_recent_df(symbol, min_bars=m1_min)
    else:
        df_m1 = get_recent_df(symbol, min_bars=limit * 5)
    if df_m1 is None or len(df_m1) < 2:
        return None
    df_m5 = resample_ohlcv(df_m1, minutes=5)
    if df_m5.empty:
        return None
    if limit:
        df_m5 = df_m5.tail(limit)
    return df_m5.reset_index(drop=True)


def get_recent_closed_df(symbol: str, limit: Optional[int] = None) -> Optional[pd.DataFrame]:
    """
    Returns recent M1 bars excluding the current forming (last) minute.
    """
    df = get_recent_df(symbol, limit or MAX_COMPLETED_BARS)
    if df is None or len(df) < 2:
        return df
    return df.iloc[:-1].reset_index(drop=True)


def get_recent_df_for_structure(symbol: str, limit: Optional[int] = None) -> Optional[pd.DataFrame]:
    """Convenience wrapper for structure analysis."""
    eff_limit = limit or MAX_COMPLETED_BARS
    df = get_recent_closed_df(symbol, eff_limit)
    if df is None or (hasattr(df, "empty") and df.empty):
        df = get_recent_df(symbol, eff_limit)
    return df


def get_buffer_status(symbol: str) -> Dict[str, Any]:
    """Debug / UI helper — includes both M1 and derived M5 buffer fill."""
    symbol = normalize_symbol(symbol)
    buf = LIVE_BARS.get(symbol, deque())
    count = len(buf)
    last = buf[-1] if buf else None
    m5_count = 0
    try:
        df_m5 = get_recent_m5_df(symbol)
        m5_count = len(df_m5) if df_m5 is not None else 0
    except:
        pass
    return {
        "symbol": symbol,
        "bars_in_buffer": count,
        "effective_bars": min(count, MAX_COMPLETED_BARS + 1),
        "max_bars": MAX_COMPLETED_BARS,
        "pct_full": round(min(count, MAX_COMPLETED_BARS + 1) / (MAX_COMPLETED_BARS + 1) * 100, 2) if (MAX_COMPLETED_BARS + 1) > 0 else 0,
        "m5_bars_in_buffer": m5_count,
        "max_m5_bars": MAX_M5_BARS,
        "m5_pct_full": round(m5_count / MAX_M5_BARS * 100, 2) if MAX_M5_BARS > 0 else 0,
        "m5_ready": m5_count >= 5,
        "ticks_received": TICK_STATS.get(symbol, 0),
        "last_tick_at": LAST_TICK_AT.get(symbol),
        "bars_completed": max(0, count - 1),
        "forming_bar": last,
        "ea_config": get_ea_config(symbol),
        "note": "Live stream only — buffer grows from EA realtime sends. The last bar is the current forming minute."
    }


def list_tracked_symbols() -> List[str]:
    """Canonical symbols that have live buffer data."""
    return sorted({normalize_symbol(s) for s in LIVE_BARS if LIVE_BARS[s]})


def seed_buffer(symbol: str, bars: list, merge: bool = True) -> int:
    """
    Load historical M1 bars into the live buffer (for backfill / restore).
    Uses the simple aggregation logic.
    """
    symbol = normalize_symbol(symbol)
    if not bars:
        return 0
    for b in bars:
        update_live_bar(symbol, b)  # reuse the aggregation
    return len(LIVE_BARS.get(symbol, []))


# For compatibility with code that expects a singleton object with methods
class _LiveBufferCompat:
    def add_market_data(self, symbol: str, data: dict) -> None:
        add_market_data(symbol, data)

    def update_live_bar(self, symbol: str, bar: dict) -> None:
        update_live_bar(symbol, bar)

    def get_recent_df(self, symbol: str, limit: Optional[int] = None, min_bars: Optional[int] = None):
        eff = min_bars or limit or 15
        return get_recent_df(symbol, min_bars=eff)

    def get_recent_bars(self, symbol: str, limit: Optional[int] = None):
        df = get_recent_df(symbol, min_bars=limit or 15)
        if df is None or df.empty:
            return []
        return df.to_dict("records")

    def get_buffer_status(self, symbol: str):
        return get_buffer_status(symbol)

    def get_recent_m5_df(self, symbol: str, limit: Optional[int] = None):
        return get_recent_m5_df(symbol, limit)

    def get_m5_bar_count(self, symbol: str) -> int:
        df = get_recent_m5_df(symbol)
        return len(df) if df is not None else 0

    def get_recent_closed_df(self, symbol: str, limit: Optional[int] = None):
        return get_recent_closed_df(symbol, limit)

    def get_recent_df_for_structure(self, symbol: str, limit: Optional[int] = None):
        return get_recent_df_for_structure(symbol, limit)

    def __call__(self, symbol: str, timeframe: Optional[str] = None):
        """Compat for legacy call sites."""
        key = normalize_symbol(symbol)
        return LIVE_BARS.get(key)

    def forming(self):
        return {}

    @property
    def buffers(self):
        return {normalize_symbol(k): v for k, v in LIVE_BARS.items()}

    def list_tracked_symbols(self):
        return list_tracked_symbols()

    @property
    def max_bars(self):
        return MAX_COMPLETED_BARS

    @property
    def max_m5_bars(self):
        return MAX_M5_BARS


live_buffer = _LiveBufferCompat()


def filter_df_to_catchup_window(df: Optional[pd.DataFrame], hours: Optional[float] = None) -> Optional[pd.DataFrame]:
    """Minimal implementation for compatibility. Returns the df as-is (realtime focused)."""
    if df is None or (hasattr(df, "empty") and df.empty):
        return df
    return df


def is_within_catchup_window(ts, hours: Optional[float] = None) -> bool:
    """True if the bar timestamp is recent enough to drive live trading decisions.
    Older data is still accepted for buffer seeding / DB but we skip heavy signal path.
    """
    if ts is None:
        return True
    try:
        ts = to_naive_utc(ts)
        cutoff = catchup_cutoff(hours)
        return ts >= cutoff
    except Exception:
        return True


def catchup_cutoff(hours: Optional[float] = None) -> datetime:
    """Cutoff for 'live' vs historical backfill. Default 8h; older posts are treated as backfill seed only."""
    hours = hours if hours is not None else 8.0
    return datetime.utcnow() - timedelta(hours=hours)


# Back-compat for any direct imports of these
def _resolve_buffer_key(symbol: str) -> str:
    return normalize_symbol(symbol)


def _normalize_buffer_timestamps(symbol: str) -> None:
    pass


print("[live_data] Using simplified real-time M1 aggregation buffer (user-provided functional version)")
