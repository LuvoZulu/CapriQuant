"""
CapriQuant CRT (Candle Range Theory) Strategy — FULLY WIRED VERSION
====================================================================

FIXES in this version:
  - Module is now imported and wired into multi_timeframe.py
  - evaluate_crt_setups() returns typed CRTSetup objects with .to_dict()
  - get_active_levels() returns serialisable dict for EA / MarketStructure.to_dict()
  - update_reference_bar() and update_price() properly update state
  - All public methods have type hints and docstrings

CRT Concepts implemented:
  - Range High (RH) / Range Low (RL): liquidity pools
  - Range Equilibrium (EQ / 50%): CE level / midpoint
  - Consequent Encroachment (CE): 50% of wick-to-body
  - High/Low Raid → Reversal setup
  - Equilibrium bounce with HTF bias
  - Expansion targets: 1.0×, 1.5×, 2.0× range

Integration (already done in multi_timeframe.py):
    crt = CRTStrategy(reference_tf="M15")
    crt.update_reference_bar(o, h, l, c, ts)
    crt.update_price(current_price)
    setups = crt.evaluate_crt_setups(current_price, htf_direction)
    levels = crt.get_active_levels()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CRT_WEIGHT = 0.25   # confluence weight when scoring into MTF signal


# ---------------------------------------------------------------------------
# CRT Range model
# ---------------------------------------------------------------------------

@dataclass
class CRTRange:
    """
    One reference-candle range with all key price levels computed.
    """
    candle_time: datetime
    timeframe: str
    range_high: float
    range_low: float
    open_price: float
    close_price: float

    # Computed on __post_init__
    direction: str = field(init=False)      # 'bullish' | 'bearish'
    range_size: float = field(init=False)
    equilibrium: float = field(init=False)  # 50% of full range
    ce_upper: float = field(init=False)     # consequent encroachment upper
    ce_lower: float = field(init=False)     # consequent encroachment lower
    body_high: float = field(init=False)
    body_low: float = field(init=False)
    exp_100: float = field(init=False)
    exp_150: float = field(init=False)
    exp_200: float = field(init=False)

    # Mutable tracking
    is_active: bool = True
    breached_high: bool = False
    breached_low: bool = False
    high_breach_price: Optional[float] = None
    low_breach_price: Optional[float] = None

    def __post_init__(self) -> None:
        self.direction = "bullish" if self.close_price >= self.open_price else "bearish"
        self.range_size = max(self.range_high - self.range_low, 1e-9)
        self.equilibrium = (self.range_high + self.range_low) / 2
        self.body_high = max(self.open_price, self.close_price)
        self.body_low = min(self.open_price, self.close_price)
        # CE = 50% of wick (distance from range extreme to body edge)
        upper_wick = self.range_high - self.body_high
        lower_wick = self.body_low - self.range_low
        self.ce_upper = self.body_high + upper_wick * 0.5
        self.ce_lower = self.body_low - lower_wick * 0.5
        # Expansion targets
        self.exp_100 = self.range_high + self.range_size   # full extension up
        self.exp_150 = self.range_high + self.range_size * 1.5
        self.exp_200 = self.range_high + self.range_size * 2.0

    def to_dict(self) -> Dict:
        return {
            "candle_time": self.candle_time.isoformat() if self.candle_time else None,
            "timeframe": self.timeframe,
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
            "direction": self.direction,
            "is_active": self.is_active,
            "breached_high": self.breached_high,
            "breached_low": self.breached_low,
        }


@dataclass
class CRTSetup:
    """One detected CRT trade setup."""
    setup_type: str          # 'high_raid_reversal' | 'low_raid_reversal' | 'eq_bounce' | 'expansion'
    direction: str           # 'BUY' | 'SELL'
    confidence: float        # 0.0–1.0
    entry_zone_high: float
    entry_zone_low: float
    stop: float
    tp1: float
    tp2: float
    rationale: str
    crt_range: CRTRange

    def to_dict(self) -> Dict:
        return {
            "setup_type": self.setup_type,
            "direction": self.direction,
            "confidence": round(self.confidence, 3),
            "entry_zone_high": round(self.entry_zone_high, 5),
            "entry_zone_low": round(self.entry_zone_low, 5),
            "stop": round(self.stop, 5),
            "tp1": round(self.tp1, 5),
            "tp2": round(self.tp2, 5),
            "rationale": self.rationale,
        }


# ---------------------------------------------------------------------------
# CRTStrategy
# ---------------------------------------------------------------------------

class CRTStrategy:
    """
    Per-symbol CRT (Candle Range Theory) strategy tracker.

    Usage:
        crt = CRTStrategy(reference_tf="M15")
        # Feed M15 bars as they close:
        crt.update_reference_bar(o, h, l, c, ts)
        crt.update_price(current_price)
        setups = crt.evaluate_crt_setups(current_price, "BUY")
        levels = crt.get_active_levels()
    """

    MAX_RANGES = 5   # keep last N reference ranges

    def __init__(self, reference_tf: str = "M15") -> None:
        self.reference_tf = reference_tf
        self._ranges: List[CRTRange] = []
        self._current_price: float = 0.0
        logger.debug("[CRT] Strategy initialised for tf=%s", reference_tf)

    def update_reference_bar(
        self,
        o: float,
        h: float,
        l: float,
        c: float,
        ts: Optional[datetime] = None,
    ) -> None:
        """
        Feed a completed reference timeframe bar.
        Call this each time a M15 (or chosen reference TF) bar closes.
        """
        if ts is None:
            ts = datetime.utcnow()
        elif not isinstance(ts, datetime):
            try:
                ts = pd.Timestamp(ts).to_pydatetime()
            except Exception:
                ts = datetime.utcnow()

        rng = CRTRange(
            candle_time=ts,
            timeframe=self.reference_tf,
            range_high=float(h),
            range_low=float(l),
            open_price=float(o),
            close_price=float(c),
        )
        self._ranges.append(rng)
        # Keep only last N
        if len(self._ranges) > self.MAX_RANGES:
            self._ranges = self._ranges[-self.MAX_RANGES:]
        logger.debug(
            "[CRT] Range added: H=%.5f L=%.5f EQ=%.5f dir=%s",
            rng.range_high, rng.range_low, rng.equilibrium, rng.direction,
        )

    def update_price(self, price: float) -> None:
        """Update current price and check for range breaches."""
        if price <= 0:
            return
        self._current_price = price
        for rng in self._ranges:
            if not rng.is_active:
                continue
            if price > rng.range_high and not rng.breached_high:
                rng.breached_high = True
                rng.high_breach_price = price
                logger.debug("[CRT] Range HIGH breached at %.5f", price)
            if price < rng.range_low and not rng.breached_low:
                rng.breached_low = True
                rng.low_breach_price = price
                logger.debug("[CRT] Range LOW breached at %.5f", price)

    def evaluate_crt_setups(
        self,
        current_price: float,
        htf_direction: str = "NEUTRAL",
    ) -> List[CRTSetup]:
        """
        Evaluate CRT setups given current price and HTF bias.

        Returns list of CRTSetup objects, sorted by confidence descending.
        """
        self.update_price(current_price)
        setups: List[CRTSetup] = []

        if not self._ranges:
            return setups

        latest = self._ranges[-1]
        price = current_price
        rng_size = latest.range_size
        atr_proxy = rng_size   # range size as ATR proxy

        # ── Setup 1: High Raid Reversal (stop hunt above range high → sell) ──
        if latest.breached_high and latest.high_breach_price is not None:
            # Price came back inside range after breaching high → bearish reversal
            if price < latest.range_high and price > latest.equilibrium:
                confidence = 0.70
                if htf_direction == "SELL":
                    confidence += 0.15
                setups.append(CRTSetup(
                    setup_type="high_raid_reversal",
                    direction="SELL",
                    confidence=min(confidence, 1.0),
                    entry_zone_high=latest.range_high,
                    entry_zone_low=latest.ce_upper,
                    stop=latest.high_breach_price + atr_proxy * 0.25,
                    tp1=latest.equilibrium,
                    tp2=latest.range_low,
                    rationale=f"CRT high raid at {latest.range_high:.5f}, price returned inside range",
                    crt_range=latest,
                ))

        # ── Setup 2: Low Raid Reversal (stop hunt below range low → buy) ──
        if latest.breached_low and latest.low_breach_price is not None:
            if price > latest.range_low and price < latest.equilibrium:
                confidence = 0.70
                if htf_direction == "BUY":
                    confidence += 0.15
                setups.append(CRTSetup(
                    setup_type="low_raid_reversal",
                    direction="BUY",
                    confidence=min(confidence, 1.0),
                    entry_zone_high=latest.ce_lower,
                    entry_zone_low=latest.range_low,
                    stop=latest.low_breach_price - atr_proxy * 0.25,
                    tp1=latest.equilibrium,
                    tp2=latest.range_high,
                    rationale=f"CRT low raid at {latest.range_low:.5f}, price returned inside range",
                    crt_range=latest,
                ))

        # ── Setup 3: Equilibrium bounce ──────────────────────────────────
        at_eq = abs(price - latest.equilibrium) < rng_size * 0.10
        if at_eq and not latest.breached_high and not latest.breached_low:
            if htf_direction == "BUY":
                confidence = 0.55
                setups.append(CRTSetup(
                    setup_type="eq_bounce",
                    direction="BUY",
                    confidence=confidence,
                    entry_zone_high=latest.ce_upper,
                    entry_zone_low=latest.equilibrium,
                    stop=latest.range_low - atr_proxy * 0.15,
                    tp1=latest.range_high,
                    tp2=latest.exp_100,
                    rationale=f"CRT EQ bounce at {latest.equilibrium:.5f} with bullish HTF",
                    crt_range=latest,
                ))
            elif htf_direction == "SELL":
                confidence = 0.55
                setups.append(CRTSetup(
                    setup_type="eq_bounce",
                    direction="SELL",
                    confidence=confidence,
                    entry_zone_high=latest.equilibrium,
                    entry_zone_low=latest.ce_lower,
                    stop=latest.range_high + atr_proxy * 0.15,
                    tp1=latest.range_low,
                    tp2=latest.range_low - rng_size,
                    rationale=f"CRT EQ bounce at {latest.equilibrium:.5f} with bearish HTF",
                    crt_range=latest,
                ))

        setups.sort(key=lambda x: x.confidence, reverse=True)
        return setups

    def get_active_levels(self) -> Dict:
        """
        Returns serialisable dict of active CRT levels for the EA.
        Attach to MarketStructure.to_dict() as 'crt_levels'.
        """
        if not self._ranges:
            return {}
        latest = self._ranges[-1]
        return {
            "range_high": round(latest.range_high, 5),
            "range_low": round(latest.range_low, 5),
            "equilibrium": round(latest.equilibrium, 5),
            "ce_upper": round(latest.ce_upper, 5),
            "ce_lower": round(latest.ce_lower, 5),
            "exp_100": round(latest.exp_100, 5),
            "exp_150": round(latest.exp_150, 5),
            "exp_200": round(latest.exp_200, 5),
            "breached_high": latest.breached_high,
            "breached_low": latest.breached_low,
            "direction": latest.direction,
        }

    def get_latest_range(self) -> Optional[CRTRange]:
        return self._ranges[-1] if self._ranges else None


# Avoid circular import for pd
try:
    import pandas as pd  # noqa: F401 (only needed inside update_reference_bar)
except ImportError:
    pass