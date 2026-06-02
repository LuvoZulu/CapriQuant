"""
Live Data Manager for real-time TICK aggregation into M1 bars + rolling buffer.

Supports the real-time POST /market-data path that returns immediate structure signals.

Buffer size set to 10080 M1 bars (~7 trading days / 1 calendar week of M1 data).
When the buffer is full, oldest bars are automatically dropped (rolling window).
The "cycle" idea: after 10080 we continue appending new bars; the deque handles the rolling.
You can optionally track a bar_index or week_cycle if you want explicit "reset counting".

Used by main.py market_data endpoint for low-latency signals on live TICKs
while still persisting bars to the DB for historical queries and the future UI.
"""

from collections import deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import pandas as pd

# =============================================================================
# CONFIG
# =============================================================================
MAX_M1_BARS = 10080  # 7 * 24 * 60 = one week of minute bars for deep structure / trend context


@dataclass
class CompletedBar:
    """Normalized completed M1 bar."""
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    timeframe: str = "M1"


class LiveDataManager:
    """
    Per-symbol rolling buffer of completed M1 bars.
    Aggregates high-frequency TICK / forming-bar payloads from the EA into clean M1 bars.
    """

    def __init__(self, max_bars: int = MAX_M1_BARS):
        self.max_bars = max_bars
        self.buffers: Dict[str, deque] = defaultdict(lambda: deque(maxlen=max_bars))
        self.forming: Dict[str, Dict[str, Any]] = {}
        self.last_minute_key: Dict[str, str] = {}
        self.stats: Dict[str, Dict] = defaultdict(lambda: {"ticks_received": 0, "bars_completed": 0})

    def _minute_key(self, dt: Optional[datetime] = None) -> str:
        if dt is None:
            dt = datetime.utcnow()
        return dt.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")

    def add_market_data(self, symbol: str, data: Dict[str, Any]) -> Optional[CompletedBar]:
        """
        Main entry point called from /market-data for every payload (especially TICKs).

        Returns a CompletedBar when a new minute boundary is crossed (i.e. previous bar finished).
        The caller can then persist that completed M1 bar to the DB.
        """
        symbol = symbol.upper()
        self.stats[symbol]["ticks_received"] += 1

        # Prefer explicit timestamp if the EA ever sends one; fall back to server time
        ts = data.get("timestamp")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                ts = datetime.utcnow()
        elif ts is None:
            ts = datetime.utcnow()

        price = float(data.get("close") or data.get("bid") or data.get("last") or data.get("price") or 0.0)
        vol = float(data.get("volume", data.get("tick_volume", 0)))

        # Also respect explicit OHLC if the EA sends the current forming bar's values
        open_p = float(data.get("open", price))
        high_p = float(data.get("high", price))
        low_p = float(data.get("low", price))
        close_p = price

        minute_key = self._minute_key(ts)

        completed: Optional[CompletedBar] = None

        current = self.forming.get(symbol)

        if current is None or current.get("minute_key") != minute_key:
            # Minute boundary crossed (or first tick for this symbol)
            if current is not None:
                # Complete the previous bar
                completed = CompletedBar(
                    symbol=symbol,
                    timestamp=current.get("timestamp", ts - timedelta(minutes=1)),
                    open=current["open"],
                    high=current["high"],
                    low=current["low"],
                    close=current["close"],
                    volume=current.get("volume", 0.0),
                )
                self.buffers[symbol].append(completed)
                self.stats[symbol]["bars_completed"] += 1

            # Start fresh forming bar
            self.forming[symbol] = {
                "minute_key": minute_key,
                "timestamp": ts,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": vol,
            }
            self.last_minute_key[symbol] = minute_key
        else:
            # Update the forming bar in the same minute
            bar = self.forming[symbol]
            bar["high"] = max(bar["high"], high_p)
            bar["low"] = min(bar["low"], low_p)
            bar["close"] = close_p
            bar["volume"] = bar.get("volume", 0.0) + vol
            # If the payload carries a more authoritative open for the bar, honor it
            if "open" in data and data["open"] is not None:
                bar["open"] = float(data["open"])

        return completed

    def get_recent_bars(self, symbol: str, limit: Optional[int] = None) -> List[CompletedBar]:
        """Return the most recent completed M1 bars (newest last). Does not include the current forming bar."""
        buf = self.buffers.get(symbol.upper(), deque())
        bars = list(buf)
        if limit:
            bars = bars[-limit:]
        return bars

    def get_recent_df(self, symbol: str, limit: Optional[int] = None) -> pd.DataFrame:
        """Convenience: bars as DataFrame ready for compute_structure / compute_market_structure.
        Includes completed bars + the current forming bar (as the latest entry) if present.
        This ensures the live buffer is useful even before a full minute completes.
        """
        bars = self.get_recent_bars(symbol, limit)  # completed only
        forming = self.forming.get(symbol.upper())
        if forming:
            forming_bar = CompletedBar(
                symbol=symbol.upper(),
                timestamp=forming.get("timestamp", datetime.utcnow()),
                open=forming["open"],
                high=forming["high"],
                low=forming["low"],
                close=forming["close"],
                volume=forming.get("volume", 0.0),
            )
            bars = bars + [forming_bar]
        if not bars:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        data = [
            {
                "timestamp": b.timestamp,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ]
        df = pd.DataFrame(data)
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    def get_buffer_status(self, symbol: str) -> Dict[str, Any]:
        buf = self.buffers.get(symbol.upper(), deque())
        completed_count = len(buf)
        has_forming = symbol.upper() in self.forming
        effective_count = completed_count + (1 if has_forming else 0)
        return {
            "symbol": symbol.upper(),
            "bars_in_buffer": completed_count,  # only completed previous minutes
            "effective_bars": effective_count,  # completed + current forming (for UI/debug)
            "max_bars": self.max_bars,
            "pct_full": round(effective_count / self.max_bars * 100, 2) if self.max_bars > 0 else 0,
            "ticks_received": self.stats[symbol.upper()]["ticks_received"],
            "bars_completed": self.stats[symbol.upper()]["bars_completed"],
            "forming_bar": self.forming.get(symbol.upper()),
        }

    def reset_symbol(self, symbol: str):
        """Utility for testing / forced restart of a symbol's buffer."""
        sym = symbol.upper()
        self.buffers[sym].clear()
        self.forming.pop(sym, None)
        self.last_minute_key.pop(sym, None)
        self.stats[sym] = {"ticks_received": 0, "bars_completed": 0}


# Singleton used by the API
live_buffer = LiveDataManager(max_bars=MAX_M1_BARS)


def get_live_buffer() -> LiveDataManager:
    """Accessor in case we want dependency injection later."""
    return live_buffer
