"""
CapriQuant SessionAMDDetector — Adaptive Volatility-Aware Phase Classification
==============================================================================
Fixes:
  - Session/AMD was purely clock-based → can misclassify in low-vol regimes
  - No realized volatility profiling
  - No news / holiday calendar
  - No adaptive manipulation detection via vol expansion

What's new:
  1. Realized volatility (log-return std) computed every bar
  2. Vol percentile ranking against rolling history → regime label
  3. News calendar blackout windows (±15 min around high-impact events)
  4. Conviction score per phase (how confident we are in the classification)
  5. Manipulation confirmed only when vol expands vs accumulation baseline
  6. Holiday / thin-liquidity guard (weekends + configurable holidays)

Usage:
    amd = SessionAMDDetector()
    # Feed economic calendar once at day start:
    amd.add_news_events_from_dicts(todays_high_impact_events)
    # On each closed bar:
    amd.push_bar(bar.high, bar.low, bar.close)
    result = amd.get_phase(bar.timestamp)
    # result.phase, result.conviction, result.is_high_impact_news
    if result.phase == AMDPhase.NEWS_BLACKOUT:
        return  # skip this bar
    if result.conviction < 0.55:
        return  # phase uncertain — skip
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class AMDPhase(str, Enum):
    ACCUMULATION  = "accumulation"
    MANIPULATION  = "manipulation"
    DISTRIBUTION  = "distribution"
    REBALANCE     = "rebalance"       # Post-distribution / between sessions
    CHOP          = "chop"            # Vol too low → no clean phase
    NEWS_BLACKOUT = "news_blackout"   # High-impact news window
    CLOSED        = "closed"          # Weekend or holiday


class VolRegime(str, Enum):
    LOW    = "low"     # < 25th percentile
    MEDIUM = "medium"  # 25–75th
    HIGH   = "high"    # > 75th
    SPIKE  = "spike"   # > 95th (possible news/stop-hunt)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class NewsEvent:
    event_time: datetime
    currency: str           # 'USD', 'XAU', 'GBP', etc.
    impact: str             # 'high' | 'medium' | 'low'
    title: str
    blackout_before_min: int = 15
    blackout_after_min: int = 15


@dataclass
class SessionWindow:
    name: str
    open_utc: time
    close_utc: time
    accum_end: time
    manip_end: time
    dist_end: time


@dataclass
class AMDResult:
    phase: AMDPhase
    session: str
    vol_regime: VolRegime
    realized_vol: float
    vol_percentile: float          # 0–100
    conviction: float              # 0.0–1.0
    is_high_impact_news: bool
    news_event: Optional[NewsEvent]
    reason: str

    def is_tradeable(self, min_conviction: float = 0.55) -> bool:
        """Quick check: is this a phase we should be trading?"""
        if self.phase in (AMDPhase.CHOP, AMDPhase.NEWS_BLACKOUT, AMDPhase.CLOSED):
            return False
        if self.conviction < min_conviction:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "phase": self.phase.value,
            "session": self.session,
            "vol_regime": self.vol_regime.value,
            "realized_vol": round(self.realized_vol, 6),
            "vol_percentile": round(self.vol_percentile, 1),
            "conviction": round(self.conviction, 3),
            "is_high_impact_news": self.is_high_impact_news,
            "news_event": self.news_event.title if self.news_event else None,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Default session windows (UTC, tuned for XAUUSD / Forex)
# ---------------------------------------------------------------------------

DEFAULT_SESSIONS: List[SessionWindow] = [
    SessionWindow(
        name="Asia",
        open_utc=time(0, 0),  close_utc=time(7, 0),
        accum_end=time(1, 30), manip_end=time(4, 30), dist_end=time(7, 0),
    ),
    SessionWindow(
        name="London",
        open_utc=time(7, 0),  close_utc=time(12, 0),
        accum_end=time(7, 30), manip_end=time(9, 30), dist_end=time(12, 0),
    ),
    SessionWindow(
        name="NY_AM",
        open_utc=time(12, 0), close_utc=time(16, 0),
        accum_end=time(12, 30), manip_end=time(13, 30), dist_end=time(16, 0),
    ),
    SessionWindow(
        name="NY_PM",
        open_utc=time(16, 0), close_utc=time(20, 0),
        accum_end=time(16, 30), manip_end=time(17, 30), dist_end=time(20, 0),
    ),
]

_WEEKEND = {5, 6}  # Saturday, Sunday

# US bank holidays (month, day) — extend as needed
_US_HOLIDAYS: set = {
    (1, 1), (7, 4), (12, 25), (12, 26),
}


# ---------------------------------------------------------------------------
# SessionAMDDetector
# ---------------------------------------------------------------------------

class SessionAMDDetector:
    """
    Adaptive AMD phase detector.

    Replaces the prior purely clock-based implementation.
    """

    def __init__(
        self,
        sessions: Optional[List[SessionWindow]] = None,
        bars_per_day: int = 288,                # 288 × 5-min = 1 day
        vol_window: int = 20,                   # bars for realised vol
        vol_history_size: int = 576,            # ~2 trading days of history
        low_vol_chop_pct: float = 20.0,         # ≤ 20th pct → CHOP
        spike_vol_pct: float = 95.0,            # ≥ 95th → SPIKE (potential news)
        manip_vol_expansion: float = 1.25,      # vol must be 25% above accum to confirm manip
        news_events: Optional[List[NewsEvent]] = None,
        holiday_dates: Optional[set] = None,    # set of date objects
        use_vol_gating: bool = True,
        use_news_blackout: bool = True,
    ):
        self.sessions = sessions or DEFAULT_SESSIONS
        self.bars_per_day = bars_per_day
        self.vol_window = vol_window
        self.vol_history_size = vol_history_size
        self.low_vol_chop_pct = low_vol_chop_pct
        self.spike_vol_pct = spike_vol_pct
        self.manip_vol_expansion = manip_vol_expansion
        self.news_events: List[NewsEvent] = news_events or []
        self.holiday_dates: set = holiday_dates or set()
        self.use_vol_gating = use_vol_gating
        self.use_news_blackout = use_news_blackout

        self._closes: Deque[float] = deque(maxlen=vol_window + 1)
        self._vol_history: Deque[float] = deque(maxlen=vol_history_size)
        self._accum_vol_baseline: Optional[float] = None   # set during accumulation phase
        self._last_phase: AMDPhase = AMDPhase.REBALANCE

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def push_bar(self, bar_high: float, bar_low: float, bar_close: float) -> None:
        """
        Feed each closed bar. Call BEFORE get_phase().
        Uses close prices for log-return vol; high/low reserved for future ATR-based mode.
        """
        self._closes.append(bar_close)
        if len(self._closes) >= 2:
            vol = self._realised_vol()
            if vol is not None and vol > 0:
                self._vol_history.append(vol)

    # ------------------------------------------------------------------
    # Phase classification
    # ------------------------------------------------------------------

    def get_phase(self, ts: datetime) -> AMDResult:
        """
        Classify AMD phase for the given UTC timestamp.
        Call after push_bar().

        Wire into live loop:
            amd.push_bar(bar.high, bar.low, bar.close)
            result = amd.get_phase(bar.utc_time)
            if not result.is_tradeable():
                continue
        """
        # --- Closed ---
        if self._is_closed(ts):
            return self._result(
                AMDPhase.CLOSED, ts, reason="Weekend or holiday", conviction=1.0
            )

        # --- News blackout ---
        if self.use_news_blackout:
            news = self._check_news(ts)
            if news:
                return self._result(
                    AMDPhase.NEWS_BLACKOUT, ts,
                    reason=f"High-impact news: {news.title}",
                    conviction=0.95,
                    news_event=news,
                )

        current_vol = self._current_vol()
        vol_pct = self._vol_percentile(current_vol)
        vol_regime = self._vol_regime(vol_pct)

        # --- Chop gate ---
        if self.use_vol_gating and vol_pct <= self.low_vol_chop_pct and len(self._vol_history) > 20:
            return self._result(
                AMDPhase.CHOP, ts,
                reason=f"Vol pct={vol_pct:.0f}% ≤ chop threshold ({self.low_vol_chop_pct:.0f}%)",
                conviction=0.70,
            )

        # --- Clock-based phase ---
        clock_phase, session_name, base_conviction = self._clock_phase(ts)

        # --- Volatility adjustment ---
        phase, conviction, reason = self._vol_adjust_phase(
            clock_phase, session_name, base_conviction,
            current_vol, vol_pct, vol_regime
        )

        self._last_phase = phase
        return self._result(phase, ts, reason=reason, conviction=conviction,
                            is_high_impact=(vol_regime == VolRegime.SPIKE))

    # ------------------------------------------------------------------
    # Calendar helpers
    # ------------------------------------------------------------------

    def add_news_event(self, event: NewsEvent) -> None:
        if event.impact == "high":
            self.news_events.append(event)

    def add_news_events_from_dicts(self, events: List[dict]) -> None:
        """
        Load from economic calendar dict list.
        Expected keys: time (ISO str), currency, impact, title.
        Only high-impact events are stored.

        Use a free calendar API (e.g. forexfactory JSON, myfxbook, etc.)
        and call this once before each trading day.
        """
        for e in events:
            if e.get("impact", "").lower() != "high":
                continue
            try:
                self.add_news_event(NewsEvent(
                    event_time=datetime.fromisoformat(e["time"]),
                    currency=e.get("currency", "USD"),
                    impact="high",
                    title=e.get("title", "Unknown"),
                    blackout_before_min=int(e.get("blackout_before_min", 15)),
                    blackout_after_min=int(e.get("blackout_after_min", 15)),
                ))
            except Exception as ex:
                logger.warning("Failed to parse news event %s: %s", e, ex)

    def clear_past_news(self, before: Optional[datetime] = None) -> None:
        """Remove stale news events (older than `before`, default: now)."""
        cutoff = before or datetime.utcnow()
        self.news_events = [
            e for e in self.news_events
            if e.event_time + timedelta(minutes=e.blackout_after_min) >= cutoff
        ]

    def add_holiday(self, d: date) -> None:
        self.holiday_dates.add(d)

    def get_session_name(self, ts: datetime) -> str:
        _, session_name, _ = self._clock_phase(ts)
        return session_name

    # ------------------------------------------------------------------
    # Private: phase logic
    # ------------------------------------------------------------------

    def _clock_phase(self, ts: datetime) -> Tuple[AMDPhase, str, float]:
        t = ts.time().replace(second=0, microsecond=0)
        for sess in self.sessions:
            if not _time_in(t, sess.open_utc, sess.close_utc):
                continue
            if _time_in(t, sess.open_utc, sess.accum_end):
                return AMDPhase.ACCUMULATION, sess.name, 0.68
            if _time_in(t, sess.accum_end, sess.manip_end):
                return AMDPhase.MANIPULATION, sess.name, 0.63
            if _time_in(t, sess.manip_end, sess.dist_end):
                return AMDPhase.DISTRIBUTION, sess.name, 0.70
        return AMDPhase.REBALANCE, "Interlude", 0.45

    def _vol_adjust_phase(
        self,
        phase: AMDPhase,
        session: str,
        base_conv: float,
        vol: float,
        vol_pct: float,
        regime: VolRegime,
    ) -> Tuple[AMDPhase, float, str]:
        conviction = base_conv
        reason = f"Clock ({session})"

        if phase == AMDPhase.ACCUMULATION:
            # Store baseline for later manipulation confirmation
            self._accum_vol_baseline = vol
            if regime == VolRegime.LOW:
                conviction += 0.08
                reason += " | low vol confirms accumulation"
            elif regime == VolRegime.HIGH:
                conviction -= 0.12
                reason += " | high vol unusual for accumulation"

        elif phase == AMDPhase.MANIPULATION:
            if self._accum_vol_baseline and vol > 0:
                expansion = vol / self._accum_vol_baseline
                if expansion >= self.manip_vol_expansion:
                    conviction += 0.15
                    reason += f" | vol expansion ×{expansion:.2f} CONFIRMED"
                else:
                    conviction -= 0.18
                    reason += f" | vol expansion ×{expansion:.2f} WEAK (need ×{self.manip_vol_expansion})"
            else:
                conviction -= 0.10
                reason += " | no accumulation baseline to compare"

        elif phase == AMDPhase.DISTRIBUTION:
            if regime in (VolRegime.HIGH, VolRegime.SPIKE):
                conviction += 0.10
                reason += f" | high vol ({vol_pct:.0f}th pct) confirms distribution"

        elif phase == AMDPhase.REBALANCE:
            if regime == VolRegime.SPIKE:
                # Possible news in off-hours
                conviction = 0.30
                reason += " | vol spike in off-hours"

        conviction = round(max(0.10, min(conviction, 0.97)), 3)
        return phase, conviction, reason

    # ------------------------------------------------------------------
    # Private: vol helpers
    # ------------------------------------------------------------------

    def _realised_vol(self) -> Optional[float]:
        closes = list(self._closes)
        if len(closes) < 2:
            return None
        log_returns = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                log_returns.append(math.log(closes[i] / closes[i - 1]))
        if not log_returns:
            return None
        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / n
        # Annualise: sqrt(252 days × bars_per_day)
        return math.sqrt(variance * 252 * self.bars_per_day)

    def _current_vol(self) -> float:
        return self._vol_history[-1] if self._vol_history else 0.0

    def _vol_percentile(self, current: float) -> float:
        if len(self._vol_history) < 10:
            return 50.0
        hist = sorted(self._vol_history)
        rank = sum(1 for v in hist if v <= current)
        return 100.0 * rank / len(hist)

    @staticmethod
    def _vol_regime(pct: float) -> VolRegime:
        if pct >= 95:
            return VolRegime.SPIKE
        if pct >= 75:
            return VolRegime.HIGH
        if pct >= 25:
            return VolRegime.MEDIUM
        return VolRegime.LOW

    # ------------------------------------------------------------------
    # Private: calendar helpers
    # ------------------------------------------------------------------

    def _is_closed(self, ts: datetime) -> bool:
        if ts.weekday() in _WEEKEND:
            return True
        d = ts.date()
        if d in self.holiday_dates:
            return True
        if (d.month, d.day) in _US_HOLIDAYS:
            return True
        return False

    def _check_news(self, ts: datetime) -> Optional[NewsEvent]:
        for evt in self.news_events:
            window_start = evt.event_time - timedelta(minutes=evt.blackout_before_min)
            window_end   = evt.event_time + timedelta(minutes=evt.blackout_after_min)
            if window_start <= ts <= window_end:
                return evt
        return None

    # ------------------------------------------------------------------
    # Private: result factory
    # ------------------------------------------------------------------

    def _result(
        self,
        phase: AMDPhase,
        ts: datetime,
        reason: str = "",
        conviction: float = 0.5,
        news_event: Optional[NewsEvent] = None,
        is_high_impact: bool = False,
    ) -> AMDResult:
        vol = self._current_vol()
        vol_pct = self._vol_percentile(vol)
        return AMDResult(
            phase=phase,
            session=self.get_session_name(ts) if phase not in (AMDPhase.CLOSED,) else "Closed",
            vol_regime=self._vol_regime(vol_pct),
            realized_vol=round(vol, 6),
            vol_percentile=round(vol_pct, 1),
            conviction=conviction,
            is_high_impact_news=is_high_impact or (news_event is not None),
            news_event=news_event,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _time_in(t: time, start: time, end: time) -> bool:
    """True if start <= t < end (handles midnight crossover)."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end