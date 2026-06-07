"""
Live Data Buffer for real-time TICK to M1 aggregation (simpler updating logic).

Supports the real-time POST /market-data path.
Buffer: caps at 1 week of M1 candles (10080) + space for another 4 days (5760) = 15840 before rewriting (dropping oldest on append).
After system turns on (post-off), only last 1440 (1 day) used for trend/structure checks (via backfill cap + filters).
The deque uses maxlen = MAX + 1 to hold completed + current forming minute.
The last entry in the buffer for a symbol is always the current forming minute and gets updated live on every call within the minute.
This makes the buffer (and debug/UI) show data immediately and the numbers update on every incoming payload.
Data comes directly from the market (via EA TICK/M1 payloads), not DB.
New bars are ALWAYS ingested/processed (deque auto-drops oldest when full).
"""

from collections import deque
from typing import Dict, Optional, Deque, Any, List
import pandas as pd
from datetime import datetime, timezone, timedelta

from app.utils.symbols import normalize_symbol, symbol_variants
from app.config import get_settings

# Per-symbol buffer of M1 bars (last one is the current forming minute, updated live)
LIVE_BARS: Dict[str, Deque[dict]] = {}

# Real per-symbol tick counters (for debug visibility)
TICK_STATS: Dict[str, int] = {}

# Storage buffer: cap at 1 week (10080 M1) + 4 days headroom (5760) = 15840 before rewriting starts.
# After system turns on (post-off / catch-up), trend/structure checks only use last 1440 (1 day) via backfill + filters.
# Full buffer allows accumulating 1 week + 4 days of candles before oldest are dropped on new appends.
MAX_COMPLETED_BARS = 10080 + 4 * 1440  # 15840
# M5: 15840 // 5 = 3168
MAX_M5_BARS = MAX_COMPLETED_BARS // 5


def catchup_max_hours() -> float:
    return float(get_settings().catchup_max_hours)


def catchup_cutoff() -> datetime:
    """Earliest bar time allowed for live trend / structure after downtime. Strictly max 1 day (no far rollback)."""
    hours = min(24.0, catchup_max_hours())
    return datetime.utcnow() - timedelta(hours=hours)


def is_within_catchup_window(ts) -> bool:
    """True if timestamp is recent enough for catch-up / post-restart trend analysis."""
    if ts is None:
        return True
    try:
        bar_ts = to_naive_utc(ts)
    except Exception:
        return False
    return bar_ts >= catchup_cutoff()


def filter_df_to_catchup_window(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """Keep only bars within the catch-up window (strict max last 1 day after system off for trend checks)."""
    if df is None or not hasattr(df, "empty") or df.empty:
        return df
    cutoff = catchup_cutoff()
    out = df.copy()
    out["timestamp"] = out["timestamp"].apply(to_naive_utc)
    out = out[out["timestamp"] >= cutoff]
    if out.empty:
        return out
    max_bars = int(get_settings().catchup_max_m1_bars)
    if len(out) > max_bars:
        out = out.tail(max_bars)
    return out.reset_index(drop=True)


def to_naive_utc(ts) -> datetime:
    """
    Normalize any timestamp to naive UTC (no tzinfo).
    Prevents pandas sort errors when mixing DB backfill + live ticks.
    (Aligned to working version for reliable minute flooring and merge on live ticks.)
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


def _floor_to_minute(ts) -> datetime:
    """Floor a timestamp to the start of its minute (naive UTC)."""
    ts = to_naive_utc(ts)
    return ts.replace(second=0, microsecond=0)


def _normalize_buffer_timestamps(symbol: str) -> None:
    """Ensure every bar in a symbol buffer uses naive UTC timestamps."""
    symbol = normalize_symbol(symbol)
    if symbol not in LIVE_BARS:
        return
    for bar in LIVE_BARS[symbol]:
        bar["timestamp"] = to_naive_utc(bar.get("timestamp"))


def normalize_all_buffers() -> None:
    for sym in list(LIVE_BARS.keys()):
        _normalize_buffer_timestamps(sym)


def _merge_alias_buffers(canonical: str) -> None:
    """Merge legacy buffer keys (e.g. XAUUSDM) into canonical XAUUSD."""
    canonical = normalize_symbol(canonical)
    if canonical in LIVE_BARS:
        return
    for alias in symbol_variants(canonical):
        if alias != canonical and alias in LIVE_BARS and LIVE_BARS[alias]:
            LIVE_BARS[canonical] = LIVE_BARS.pop(alias)
            TICK_STATS[canonical] = TICK_STATS.get(alias, 0)
            TICK_STATS.pop(alias, None)
            return


def _resolve_buffer_key(symbol: str) -> str:
    canonical = normalize_symbol(symbol)
    _merge_alias_buffers(canonical)
    if canonical in LIVE_BARS and LIVE_BARS[canonical]:
        return canonical
    for alias in symbol_variants(canonical):
        if alias in LIVE_BARS and LIVE_BARS[alias]:
            if alias != canonical:
                LIVE_BARS[canonical] = LIVE_BARS.pop(alias)
            return canonical
    return canonical


def add_market_data(symbol: str, data: dict) -> None:
    """
    Feed new market data (from TICK payloads) into the live buffer.
    This function aggregates into per-minute bars.
    The last bar for the symbol is the current minute and is updated in place on every call within the minute.
    Call this on every incoming payload from the EA.
    If data has _quality_bad (from main ingest gate), still buffers (for diagnostics) but callers should prefer skipping structure on bad data.
    """
    symbol = _resolve_buffer_key(symbol)

    if symbol not in LIVE_BARS:
        # maxlen = MAX+1 (15841) to hold 1w+4d history + forming. New market bars always appended (oldest dropped when full).
        LIVE_BARS[symbol] = deque(maxlen=MAX_COMPLETED_BARS + 1)

    TICK_STATS[symbol] = TICK_STATS.get(symbol, 0) + 1

    buffer = LIVE_BARS[symbol]

    # Ensure buffer uses current MAX (in case code/config changed without restart)
    current_maxlen = MAX_COMPLETED_BARS + 1
    if getattr(buffer, 'maxlen', None) != current_maxlen:
        data_list = list(buffer)
        buffer = deque(data_list[-current_maxlen:], maxlen=current_maxlen)
        LIVE_BARS[symbol] = buffer

    ts_raw = data.get("timestamp")
    ts = to_naive_utc(ts_raw) if ts_raw is not None else datetime.utcnow()

    # Use the M1 bar data sent by the EA (open/high/low/close/volume for current minute)
    # or fall back to the tick price
    close = float(data.get("close", 0))
    open_ = float(data.get("open", close))
    high = float(data.get("high", close))
    low = float(data.get("low", close))
    volume = float(data.get("volume", 0))

    bar_minute = _floor_to_minute(ts)

    incoming_bar = {
        "timestamp": bar_minute,
        "open": open_,
        "high": max(high, open_, close),
        "low": min(low, open_, close),
        "close": close,
        "volume": volume,
    }

    if not buffer:
        # First ever bar for this symbol
        buffer.append(incoming_bar)
        return

    last_bar = buffer[-1]
    last_minute = _floor_to_minute(last_bar["timestamp"])

    if last_minute == bar_minute:
        # Still inside the same minute → update the current forming bar live
        _merge_bar(last_bar, incoming_bar, replace_open=False)
    else:
        # New minute (or historical older) → append (for live new minute) or upsert for old backfill
        if bar_minute > last_minute:
            buffer.append(incoming_bar)
        else:
            _upsert_historical_bar(symbol, incoming_bar)


def _merge_bar(existing: dict, incoming: dict, replace_open: bool = False) -> None:
    """Merge a duplicate-minute bar without double-counting cumulative MT5 volume."""
    if replace_open:
        existing["open"] = incoming["open"]
    existing["high"] = max(float(existing.get("high", 0)), float(incoming.get("high", 0)))
    existing["low"] = min(float(existing.get("low", 0)), float(incoming.get("low", 0)))
    existing["close"] = float(incoming.get("close", existing.get("close", 0)))
    # MT5 iVolume is usually cumulative for the forming bar. Use max instead
    # of addition so repeated posts of the same minute do not inflate volume.
    existing["volume"] = max(float(existing.get("volume", 0)), float(incoming.get("volume", 0)))


def _upsert_historical_bar(symbol: str, bar: dict) -> None:
    """Insert or replace a backfilled bar while preserving chronological order."""
    buffer = LIVE_BARS[symbol]
    bars = list(buffer)
    ts = bar["timestamp"]

    for idx, existing in enumerate(bars):
        existing_ts = _floor_to_minute(existing["timestamp"])
        if existing_ts == ts:
            _merge_bar(existing, bar, replace_open=True)
            bars[idx] = existing
            LIVE_BARS[symbol] = deque(bars[-(MAX_COMPLETED_BARS + 1):], maxlen=MAX_COMPLETED_BARS + 1)
            return
        if existing_ts > ts:
            bars.insert(idx, bar)
            LIVE_BARS[symbol] = deque(bars[-(MAX_COMPLETED_BARS + 1):], maxlen=MAX_COMPLETED_BARS + 1)
            return

    bars.append(bar)
    LIVE_BARS[symbol] = deque(bars[-(MAX_COMPLETED_BARS + 1):], maxlen=MAX_COMPLETED_BARS + 1)


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


def get_recent_df(symbol: str, limit: Optional[int] = None, min_bars: Optional[int] = None) -> Optional[pd.DataFrame]:
    """
    Returns a DataFrame of recent M1 bars (including the current forming minute as the last row).
    Ready to be fed into compute_structure.
    Accepts limit or legacy min_bars kwarg.
    """
    symbol = _resolve_buffer_key(symbol)
    if symbol not in LIVE_BARS:
        return None

    buffer = LIVE_BARS[symbol]
    if len(buffer) < 1:
        return None

    bars = list(buffer)
    eff_limit = limit if limit is not None else min_bars
    if eff_limit:
        bars = bars[-eff_limit:]

    df = pd.DataFrame(bars)
    df["timestamp"] = df["timestamp"].apply(to_naive_utc)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def get_latest_price(symbol: str) -> Optional[dict]:
    """Returns the most recent close + timestamp from the live buffer (the current forming bar)."""
    symbol = _resolve_buffer_key(symbol)
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
    symbol = _resolve_buffer_key(symbol)
    return len(LIVE_BARS.get(symbol, []))


def get_m5_bar_count(symbol: str) -> int:
    """How many M5 candles are available from the current M1 rolling buffer."""
    df_m1 = get_recent_df(symbol)
    if df_m1 is None or len(df_m1) < 2:
        return 0
    df_m5 = resample_ohlcv(df_m1, minutes=5)
    return len(df_m5)


def get_recent_m5_df(symbol: str, limit: Optional[int] = None) -> Optional[pd.DataFrame]:
    """M5 OHLCV derived from the live M1 buffer (used by MTF structure engine)."""
    df_m1 = get_recent_df(symbol, limit=MAX_COMPLETED_BARS if limit is None else limit * 5)
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
    Strongly recommended for live structure calls to avoid spurious swings, BOS/CHOCH, OBs, FVGs and CRT from the still-updating bar.
    """
    df = get_recent_df(symbol, limit)
    if df is None or len(df) < 2:
        return df
    return df.iloc[:-1].reset_index(drop=True)


def get_recent_df_for_structure(symbol: str, limit: Optional[int] = None) -> Optional[pd.DataFrame]:
    """
    Convenience wrapper for structure analysis.
    Uses full buffer (1 week + 4 days headroom). After system turns on, only 1440 initially available from backfill.
    No forced 1-day cap for normal operation (buffer maxlen + backfill handle the post-off 1440 limit).
    """
    eff_limit = limit
    if eff_limit is None:
        eff_limit = MAX_COMPLETED_BARS
    else:
        eff_limit = min(eff_limit, MAX_COMPLETED_BARS)

    df = get_recent_closed_df(symbol, eff_limit)
    if df is None or (hasattr(df, "empty") and df.empty):
        df = get_recent_df(symbol, eff_limit)
    # No longer apply catchup filter here (would limit to 1d always). Post-off limited by available backfilled data.
    return df


def get_buffer_status(symbol: str) -> Dict[str, Any]:
    """Debug / UI helper — includes both M1 and derived M5 buffer fill. Buffer stores up to 1 week + 4 days headroom (15840 M1) before rewriting. After system on, trend/structure only 1440 (1 day) from market. Displays show actual stored vs cap."""
    symbol = _resolve_buffer_key(symbol)
    buf = LIVE_BARS.get(symbol, deque())
    count = len(buf)
    last = buf[-1] if buf else None
    m5_count = get_m5_bar_count(symbol)
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
        "bars_completed": max(0, count - 1),
        "forming_bar": last,
        "note": "bars_in_buffer can reach up to MAX+1 (15841) for 1w+4d + forming. New market bars ALWAYS processed/appended (deque drops oldest when full). After off, trend/structure capped to 1440 via backfill/filters. Full buffer for normal op."
    }


def get_all_buffer_lengths() -> dict:
    """Returns how many bars are currently stored for each symbol (canonical keys)."""
    out: Dict[str, int] = {}
    for sym, buf in LIVE_BARS.items():
        key = normalize_symbol(sym)
        out[key] = max(out.get(key, 0), len(buf))
    return out


def list_tracked_symbols() -> List[str]:
    """Canonical symbols that have live buffer data."""
    return sorted({normalize_symbol(s) for s in LIVE_BARS if LIVE_BARS[s]})


def seed_buffer(symbol: str, bars: list, merge: bool = True) -> int:
    """
    Load historical M1 bars into the live buffer (startup backfill / disk restore).
    """
    symbol = normalize_symbol(symbol)
    if not bars:
        return 0

    normalized = []
    for b in bars:
        ts = to_naive_utc(b.get("timestamp"))
        normalized.append({
            "timestamp": ts,
            "open": float(b.get("open", 0)),
            "high": float(b.get("high", 0)),
            "low": float(b.get("low", 0)),
            "close": float(b.get("close", 0)),
            "volume": float(b.get("volume", 0)),
        })

    normalized.sort(key=lambda x: x["timestamp"])
    cutoff = catchup_cutoff()
    normalized = [b for b in normalized if b["timestamp"] >= cutoff]
    if not normalized:
        return 0
    # dedupe by minute
    deduped = []
    seen = set()
    for b in normalized:
        key = b["timestamp"]
        if key in seen:
            deduped[-1] = b
        else:
            seen.add(key)
            deduped.append(b)

    if merge and symbol in LIVE_BARS and LIVE_BARS[symbol]:
        existing = {b["timestamp"]: b for b in LIVE_BARS[symbol]}
        for b in deduped:
            existing[b["timestamp"]] = b
        merged = sorted(existing.values(), key=lambda x: x["timestamp"])
        LIVE_BARS[symbol] = deque(merged[-(MAX_COMPLETED_BARS + 1):], maxlen=MAX_COMPLETED_BARS + 1)
    else:
        LIVE_BARS[symbol] = deque(deduped[-(MAX_COMPLETED_BARS + 1):], maxlen=MAX_COMPLETED_BARS + 1)

    _normalize_buffer_timestamps(symbol)
    return len(LIVE_BARS[symbol])


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

    def get_recent_df(self, symbol: str, limit: Optional[int] = None, min_bars: Optional[int] = None):
        return get_recent_df(symbol, limit, min_bars)

    def get_recent_bars(self, symbol: str, limit: Optional[int] = None):
        df = get_recent_df(symbol, limit)
        if df is None or df.empty:
            return []
        return df.to_dict("records")

    def get_buffer_status(self, symbol: str):
        return get_buffer_status(symbol)

    def get_recent_m5_df(self, symbol: str, limit: Optional[int] = None):
        return get_recent_m5_df(symbol, limit)

    def get_m5_bar_count(self, symbol: str) -> int:
        return get_m5_bar_count(symbol)

    def get_recent_closed_df(self, symbol: str, limit: Optional[int] = None):
        return get_recent_closed_df(symbol, limit)

    def get_recent_df_for_structure(self, symbol: str, limit: Optional[int] = None):
        return get_recent_df_for_structure(symbol, limit)

    def __call__(self, symbol: str, timeframe: Optional[str] = None):
        """Compat for legacy call sites: live_buffer(symbol, "M1") -> raw deque (or None)."""
        key = _resolve_buffer_key(symbol)
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
