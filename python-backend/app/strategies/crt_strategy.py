"""
CapriQuant CRT (Candle Range Theory) Strategy
=============================================
Fixes:
  - Module existed but was never imported or wired
  - No connection to confluence.py / evaluate_setups
  - MarketStructure.to_dict() did not expose CRT levels
  - No range levels available for backtesting

CRT Concepts implemented:
  - Range High (RH) / Range Low (RL): liquidity pools
  - Range Equilibrium (50%): CE level / midpoint
  - Consequent Encroachment (CE): 50% of wick-to-body
  - High/Low Raid → Reversal setup
  - Equilibrium bounce with HTF bias
  - Expansion targets: 1.0×, 1.5×, 2.0× range

Integration (add to confluence.py):
    from crt_strategy import CRTStrategy
    self.crt = CRTStrategy(reference_tf="M15")

    # On each M15 bar close:
    self.crt.update_reference_bar(o, h, l, c, ts)
    self.crt.update_price(current_price)

    # In evaluate_setups / combine_mtf_signals:
    crt_setups = self.crt.evaluate_crt_setups(current_price, htf_direction)
    for s in crt_setups:
        confluence_score += s.confidence * CRT_WEIGHT

    # In MarketStructure.to_dict():
    data["crt_levels"] = self.crt.get_active_levels()
    data["crt_setups"] = [s.to_dict() for s in crt_setups]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Weight for confluence scoring (tune per your system)
CRT_WEIGHT = 0.25


# ---------------------------------------------------------------------------
# CRT Range model
# ---------------------------------------------------------------------------

@dataclass
class CRTRange:
    """
    One reference-candle range.
    All key price levels computed from OHLC of the reference candle.
    """
    candle_time: datetime
    timeframe: str
    range_high: float
    range_low: float
    open_price: float
    close_price: float

    # Computed on init
    direction: str = field(init=False)      # 'bullish' | 'bearish'
    range_size: float = field(init=False)
    equilibrium: float = field(init=False)  # 50% of full range
    ce_upper: float = field(init=False)     # consequent encroachment upper
    ce_lower: float = field(init=False)     # consequent encroachment lower
    body_high: float = field(init=False)
    body_low: float = field(init=False)
    exp_100: float = field(init=False)      # Full range extension from boundary
    exp_150: float = field(init=False)      # 1.5× extension
    exp_200: float = field(init=False)      # 2.0× extension

    # Mutable tracking
    is_active: bool = True
    breached_high: bool = False
    breached_low: bool = False
    high_breach_price: Optional[float] = None
    low_breach_price: Optional[float] = None

    def __post_init__(self) -> None:
        self.direction = "bullish" if self.close_price >= self.open_price else "bearish"
        self.range_size = self.range_high - self.range_low
        self.equilibrium = self.range_low + self.range_size * 0.5
        self.body_high = max(self.open_price, self.close_price)
        self.body_low = min(self.open_price, self.close_price)

        # CE levels: 50% of each wick
        upper_wick = self.range_high - self.body_high
        lower_wick = self.body_low - self.range_low
        self.ce_upper = self.body_high + upper_wick * 0.5
        self.ce_lower = self.range_low + lower_wick * 0.5

        # Expansion targets from breach boundary
        if self.direction == "bullish":
            self.exp_100 = self.range_high + self.range_size
            self.exp_150 = self.range_high + self.range_size * 1.5
            self.exp_200 = self.range_high + self.range_size * 2.0
        else:
            self.exp_100 = self.range_low - self.range_size
            self.exp_150 = self.range_low - self.range_size * 1.5
            self.exp_200 = self.range_low - self.range_size * 2.0

    def to_dict(self) -> dict:
        return {
            "type": "crt_range",
            "candle_time": self.candle_time.isoformat(),
            "timeframe": self.timeframe,
            "direction": self.direction,
            "range_high": round(self.range_high, 5),
            "range_low": round(self.range_low, 5),
            "equilibrium": round(self.equilibrium, 5),
            "ce_upper": round(self.ce_upper, 5),
            "ce_lower": round(self.ce_lower, 5),
            "body_high": round(self.body_high, 5),
            "body_low": round(self.body_low, 5),
            "exp_100": round(self.exp_100, 5),
            "exp_150": round(self.exp_150, 5),
            "exp_200": round(self.exp_200, 5),
            "range_size": round(self.range_size, 5),
            "is_active": self.is_active,
            "breached_high": self.breached_high,
            "breached_low": self.breached_low,
        }


# ---------------------------------------------------------------------------
# CRT Setup (tradeable signal)
# ---------------------------------------------------------------------------

@dataclass
class CRTSetup:
    """
    A tradeable setup derived from CRT analysis.
    Returned by evaluate_crt_setups() for use in confluence scoring.
    """
    crt_range: CRTRange
    setup_type: str       # 'crt_high_raid' | 'crt_low_raid' | 'crt_eq_bounce'
    direction: str        # 'long' | 'short'
    entry_high: float
    entry_low: float
    stop: float
    tp1: float            # EQ or range midpoint
    tp2: float            # Range opposite boundary
    tp3: float            # Expansion target
    confidence: float     # 0.0 – 1.0 for confluence weighting
    reason: str = ""

    @property
    def sl_pts(self) -> float:
        if self.direction == "long":
            return self.entry_low - self.stop
        return self.stop - self.entry_high

    @property
    def rr_to_tp2(self) -> float:
        if self.sl_pts <= 0:
            return 0.0
        mid_entry = (self.entry_high + self.entry_low) / 2
        tp_pts = abs(self.tp2 - mid_entry)
        return tp_pts / self.sl_pts

    def to_dict(self) -> dict:
        return {
            "setup_type": self.setup_type,
            "direction": self.direction,
            "entry_high": round(self.entry_high, 5),
            "entry_low": round(self.entry_low, 5),
            "stop": round(self.stop, 5),
            "tp1": round(self.tp1, 5),
            "tp2": round(self.tp2, 5),
            "tp3": round(self.tp3, 5),
            "confidence": round(self.confidence, 3),
            "rr_to_tp2": round(self.rr_to_tp2, 2),
            "reason": self.reason,
            "crt_levels": self.crt_range.to_dict(),
        }


# ---------------------------------------------------------------------------
# CRTStrategy — main class
# ---------------------------------------------------------------------------

class CRTStrategy:
    """
    Candle Range Theory strategy module.

    Attach one instance to your MarketStructure or ConfluenceEvaluator:
        self.crt = CRTStrategy(reference_tf="M15")

    Feed it reference TF bars and query it for setups on lower TF signals.
    """

    def __init__(
        self,
        reference_tf: str = "M15",
        min_range_pts: float = 3.0,          # Ignore tiny ranges (noise)
        max_active_ranges: int = 4,
        eq_tolerance_pct: float = 0.0015,    # ±0.15% EQ zone
        breach_buffer_pct: float = 0.0005,   # 0.05% through boundary = confirmed breach
        stop_buffer_pts: float = 2.0,        # Stop buffer beyond range boundary
        min_rr_to_add: float = 1.5,          # Only add setups with RR >= this
    ):
        self.reference_tf = reference_tf
        self.min_range_pts = min_range_pts
        self.max_active_ranges = max_active_ranges
        self.eq_tolerance_pct = eq_tolerance_pct
        self.breach_buffer_pct = breach_buffer_pct
        self.stop_buffer_pts = stop_buffer_pts
        self.min_rr_to_add = min_rr_to_add
        self._ranges: List[CRTRange] = []

    # ------------------------------------------------------------------
    # Ingestion — call on each reference TF bar close
    # ------------------------------------------------------------------

    def update_reference_bar(
        self,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_time: datetime,
    ) -> Optional[CRTRange]:
        """
        Register a new reference-timeframe closed bar.
        Returns the CRTRange if it was accepted (above min_range_pts), else None.

        Wire in your M15 bar handler:
            self.crt.update_reference_bar(bar.open, bar.high, bar.low, bar.close, bar.time)
        """
        rng_size = bar_high - bar_low
        if rng_size < self.min_range_pts:
            return None

        rng = CRTRange(
            candle_time=bar_time,
            timeframe=self.reference_tf,
            range_high=bar_high,
            range_low=bar_low,
            open_price=bar_open,
            close_price=bar_close,
        )
        self._ranges.append(rng)
        # Keep only active ranges, capped at max
        self._ranges = [r for r in self._ranges if r.is_active][-self.max_active_ranges:]
        logger.debug(
            "CRT range | %s H=%.3f L=%.3f EQ=%.3f dir=%s",
            bar_time, bar_high, bar_low, rng.equilibrium, rng.direction,
        )
        return rng

    def update_price(self, current_price: float) -> None:
        """
        Track price vs range boundaries to detect breaches.
        Call on each M5/M1 bar close (or tick in live mode).
        """
        for rng in self._ranges:
            if not rng.is_active:
                continue
            breach_pts = rng.range_size * self.breach_buffer_pct

            if not rng.breached_high and current_price > rng.range_high + breach_pts:
                rng.breached_high = True
                rng.high_breach_price = current_price
                logger.debug("CRT HIGH breached @ %.3f (range H=%.3f)", current_price, rng.range_high)

            if not rng.breached_low and current_price < rng.range_low - breach_pts:
                rng.breached_low = True
                rng.low_breach_price = current_price
                logger.debug("CRT LOW breached @ %.3f (range L=%.3f)", current_price, rng.range_low)

            # Deactivate when both sides raided (range fully consumed)
            if rng.breached_high and rng.breached_low:
                rng.is_active = False

    # ------------------------------------------------------------------
    # Setup evaluation — wire into confluence.py / evaluate_setups
    # ------------------------------------------------------------------

    def evaluate_crt_setups(
        self,
        current_price: float,
        htf_direction: Optional[str] = None,   # 'bullish' | 'bearish' from M15 bias
    ) -> List[CRTSetup]:
        """
        Evaluates current price against all active CRT ranges.
        Returns a list of valid CRTSetup objects.

        Wire into confluence.py:
            crt_setups = self.crt.evaluate_crt_setups(current_price, self.htf_bias)
            for setup in crt_setups:
                score += setup.confidence * CRT_WEIGHT
                all_setups.append({"source": "crt", **setup.to_dict()})
        """
        setups: List[CRTSetup] = []

        for rng in self._ranges:
            if not rng.is_active:
                continue

            # === Pattern 1: High Raid — short reversal ===
            # Price swept above range high, now back inside or at upper zone
            if rng.breached_high and not rng.breached_low:
                if htf_direction in (None, "bearish"):
                    s = self._build_high_raid_setup(rng, current_price)
                    if s and s.rr_to_tp2 >= self.min_rr_to_add:
                        setups.append(s)

            # === Pattern 2: Low Raid — long reversal ===
            # Price swept below range low, now back inside or at lower zone
            if rng.breached_low and not rng.breached_high:
                if htf_direction in (None, "bullish"):
                    s = self._build_low_raid_setup(rng, current_price)
                    if s and s.rr_to_tp2 >= self.min_rr_to_add:
                        setups.append(s)

            # === Pattern 3: Equilibrium Bounce ===
            # Price at 50% of range — fade with HTF bias
            eq_hi = rng.equilibrium * (1 + self.eq_tolerance_pct)
            eq_lo = rng.equilibrium * (1 - self.eq_tolerance_pct)
            if eq_lo <= current_price <= eq_hi and htf_direction:
                s = self._build_eq_setup(rng, current_price, htf_direction)
                if s and s.rr_to_tp2 >= self.min_rr_to_add:
                    setups.append(s)

        return setups

    def get_active_levels(self) -> List[dict]:
        """
        All active CRT price levels for display and serialisation.

        Wire into MarketStructure.to_dict():
            data["crt_levels"] = self.crt.get_active_levels()
        """
        return [r.to_dict() for r in self._ranges if r.is_active]

    def get_active_ranges(self) -> List[CRTRange]:
        return [r for r in self._ranges if r.is_active]

    # ------------------------------------------------------------------
    # Private setup builders
    # ------------------------------------------------------------------

    def _build_high_raid_setup(
        self, rng: CRTRange, current_price: float
    ) -> Optional[CRTSetup]:
        """Short setup: range high was swept, expect reversal to EQ / range low."""
        # Entry zone: from range high down to CE upper (retracement zone)
        entry_high = rng.range_high + rng.range_size * 0.08
        entry_low = rng.ce_upper

        if not (entry_low <= current_price <= entry_high):
            return None

        stop = round(rng.range_high + self.stop_buffer_pts, 5)
        tp1 = round(rng.equilibrium, 5)
        tp2 = round(rng.range_low, 5)
        # If range itself is bearish, extend to full expansion; else to range low
        tp3 = round(rng.exp_100 if rng.direction == "bearish" else rng.range_low - rng.range_size * 0.5, 5)

        # Higher confidence when range candle direction aligns (bearish range = bearish bias)
        confidence = 0.78 if rng.direction == "bearish" else 0.62

        return CRTSetup(
            crt_range=rng,
            setup_type="crt_high_raid",
            direction="short",
            entry_high=round(entry_high, 5),
            entry_low=round(entry_low, 5),
            stop=stop,
            tp1=tp1, tp2=tp2, tp3=tp3,
            confidence=confidence,
            reason=f"CRT high raid → short | rng_dir={rng.direction}",
        )

    def _build_low_raid_setup(
        self, rng: CRTRange, current_price: float
    ) -> Optional[CRTSetup]:
        """Long setup: range low was swept, expect reversal to EQ / range high."""
        entry_low = rng.range_low - rng.range_size * 0.08
        entry_high = rng.ce_lower

        if not (entry_low <= current_price <= entry_high):
            return None

        stop = round(rng.range_low - self.stop_buffer_pts, 5)
        tp1 = round(rng.equilibrium, 5)
        tp2 = round(rng.range_high, 5)
        tp3 = round(rng.exp_100 if rng.direction == "bullish" else rng.range_high + rng.range_size * 0.5, 5)

        confidence = 0.78 if rng.direction == "bullish" else 0.62

        return CRTSetup(
            crt_range=rng,
            setup_type="crt_low_raid",
            direction="long",
            entry_high=round(entry_high, 5),
            entry_low=round(entry_low, 5),
            stop=stop,
            tp1=tp1, tp2=tp2, tp3=tp3,
            confidence=confidence,
            reason=f"CRT low raid → long | rng_dir={rng.direction}",
        )

    def _build_eq_setup(
        self, rng: CRTRange, current_price: float, htf_direction: str
    ) -> Optional[CRTSetup]:
        """Bounce at range EQ with HTF bias."""
        eq_hi = round(rng.equilibrium * (1 + self.eq_tolerance_pct), 5)
        eq_lo = round(rng.equilibrium * (1 - self.eq_tolerance_pct), 5)

        if htf_direction == "bullish":
            direction = "long"
            stop = round(rng.range_low - self.stop_buffer_pts, 5)
            tp1 = round(rng.ce_upper, 5)
            tp2 = round(rng.range_high, 5)
            tp3 = round(rng.exp_100 if rng.direction == "bullish" else rng.range_high + rng.range_size * 0.5, 5)
        elif htf_direction == "bearish":
            direction = "short"
            stop = round(rng.range_high + self.stop_buffer_pts, 5)
            tp1 = round(rng.ce_lower, 5)
            tp2 = round(rng.range_low, 5)
            tp3 = round(rng.exp_100 if rng.direction == "bearish" else rng.range_low - rng.range_size * 0.5, 5)
        else:
            return None

        # EQ setups are lower confidence — require more confluence
        confidence = 0.58

        return CRTSetup(
            crt_range=rng,
            setup_type="crt_eq_bounce",
            direction=direction,
            entry_high=eq_hi,
            entry_low=eq_lo,
            stop=stop,
            tp1=tp1, tp2=tp2, tp3=tp3,
            confidence=confidence,
            reason=f"CRT EQ bounce {direction} | HTF={htf_direction}",
        )