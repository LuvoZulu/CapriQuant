"""
Market Structure Analysis Engine for CapriQuant

Core primitives for professional-grade price action / SMC-style analysis:
- Proper swing / pivot detection (not fixed-window rolling max)
- Market structure labeling (HH/HL/LH/LL) + BOS / CHOCH detection
- Order Block identification (mitigation-aware)
- Liquidity pool detection (equal highs/lows + clusters)
- Fair Value Gap (imbalance) detection
- Displacement candle measurement
- Session-aware range analysis (real AMD for indices/gold)

Design goals:
- All functions are pure where possible (take DataFrame, return structured output)
- Suitable for backtesting and live use
- Demote reliance on lagging oscillators (EMA/RSI/MACD removed from core)
- Optimized for XAUUSD, US30, NAS100, GER30 on M5-H1

No external TA libs required — pure pandas + numpy.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Literal, Tuple
from datetime import datetime, time
import pandas as pd
import numpy as np


# =============================================================================
# TYPES
# =============================================================================

SwingType = Literal["HIGH", "LOW"]
StructureBias = Literal["BULLISH", "BEARISH", "NEUTRAL", "CHOP"]
SessionPhase = Literal["ASIAN", "LONDON_OPEN", "NY_OPEN", "NY_PM", "OFF_SESSION", "UNKNOWN"]


@dataclass
class Swing:
    idx: int                    # integer position in the dataframe
    timestamp: pd.Timestamp
    price: float
    swing_type: SwingType
    strength: float             # 0.0 - 1.0 (how many bars confirmed + relative size)
    is_confirmed: bool = True


@dataclass
class StructureBreak:
    idx: int
    timestamp: pd.Timestamp
    break_type: Literal["BOS", "CHOCH"]
    direction: Literal["BULL", "BEAR"]  # BULL = broke resistance (prior high)
    broken_price: float
    confirming_price: float


@dataclass
class OrderBlock:
    idx: int
    timestamp: pd.Timestamp
    ob_type: Literal["BULLISH", "BEARISH"]
    high: float
    low: float
    origin_swing_idx: Optional[int]
    displacement_size: float      # ATR multiple of the impulsive move
    is_mitigated: bool = False
    mitigation_idx: Optional[int] = None
    strength: float = 1.0         # based on displacement + volume if available


@dataclass
class LiquidityLevel:
    price: float
    level_type: Literal["EQUAL_HIGHS", "EQUAL_LOWS", "SWING_CLUSTER"]
    count: int                    # how many touches / equal points
    strength: float
    last_touched_idx: int


@dataclass
class FairValueGap:
    idx: int
    timestamp: pd.Timestamp
    fvg_type: Literal["BULLISH", "BEARISH"]
    upper: float
    lower: float
    size: float
    is_filled: bool = False
    fill_idx: Optional[int] = None


@dataclass
class SessionRange:
    phase: SessionPhase
    asian_high: Optional[float] = None
    asian_low: Optional[float] = None
    asian_range: Optional[float] = None
    is_expanded: bool = False
    expansion_direction: Optional[Literal["UP", "DOWN"]] = None
    manipulation_detected: bool = False   # London sweep of Asian range


@dataclass
class MarketStructure:
    """The single source of truth for a given snapshot of candles."""
    symbol: str
    timeframe: str
    last_bar_idx: int
    last_bar_time: pd.Timestamp

    # Core structure
    swings: List[Swing] = field(default_factory=list)
    breaks: List[StructureBreak] = field(default_factory=list)
    bias: StructureBias = "NEUTRAL"

    # SMC elements
    order_blocks: List[OrderBlock] = field(default_factory=list)
    liquidity_levels: List[LiquidityLevel] = field(default_factory=list)
    fvgs: List[FairValueGap] = field(default_factory=list)

    # Session / AMD context
    session: SessionRange = field(default_factory=lambda: SessionRange(phase="UNKNOWN"))

    # Displacement context
    recent_displacement: Optional[Dict] = None   # last strong move details

    # Convenience
    current_price: float = 0.0
    atr: float = 0.0

    def to_dict(self) -> Dict:
        """JSON serializable summary (for API responses)."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bias": self.bias,
            "session_phase": self.session.phase,
            "manipulation_detected": self.session.manipulation_detected,
            "current_price": round(self.current_price, 5),
            "atr": round(self.atr, 5),
            "swing_count": len(self.swings),
            "active_bullish_obs": len([ob for ob in self.order_blocks if ob.ob_type == "BULLISH" and not ob.is_mitigated]),
            "active_bearish_obs": len([ob for ob in self.order_blocks if ob.ob_type == "BEARISH" and not ob.is_mitigated]),
            "unfilled_bull_fvgs": len([f for f in self.fvgs if f.fvg_type == "BULLISH" and not f.is_filled]),
            "unfilled_bear_fvgs": len([f for f in self.fvgs if f.fvg_type == "BEARISH" and not f.is_filled]),
            "liquidity_levels": len(self.liquidity_levels),
            "recent_bos_choch": [
                {"type": b.break_type, "direction": b.direction, "price": round(b.broken_price, 5)}
                for b in self.breaks[-3:]
            ],
        }


# =============================================================================
# CORE HELPERS
# =============================================================================

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Classic ATR (Wilder smoothing via RMA style using EWM)."""
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    # Use EWM with alpha = 1/period for approximate Wilder behavior
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    return atr


def detect_displacement(df: pd.DataFrame, atr: pd.Series, min_mult: float = 1.7) -> pd.Series:
    """
    A candle is 'displacing' if its range is large relative to ATR
    AND it closes near the extreme (strong momentum, not doji).
    """
    body = (df["close"] - df["open"]).abs()
    candle_range = df["high"] - df["low"]
    close_pos = (df["close"] - df["low"]) / candle_range.replace(0, np.nan)

    strong_range = candle_range > (atr * min_mult)
    strong_close = (close_pos > 0.75) | (close_pos < 0.25)

    return strong_range & strong_close


# =============================================================================
# SWING / PIVOT DETECTION (THE FOUNDATION)
# =============================================================================

def find_swings(
    df: pd.DataFrame,
    left: int = 3,   # Lowered from 5 for faster structure detection during live data accumulation
    right: int = 3,  # Lowered from 5 for faster structure detection during live data accumulation
    min_strength: float = 0.4,
    atr_period: int = 14,
) -> List[Swing]:
    """
    Identify confirmed swing highs and lows using left/right bar confirmation.

    A swing high requires the bar to be strictly higher than `left` bars before
    and `right` bars after. Same logic inverted for lows.

    Defaults lowered to 3/3 to allow swing detection earlier while data feeders are still accumulating history.

    Strength is a combination of:
    - Confirmation width (more bars = stronger)
    - Size relative to recent ATR

    This replaces the broken `high.iloc[-20:].max()` approach.
    """
    if len(df) < left + right + 2:
        return []

    highs = df["high"].values
    lows = df["low"].values
    timestamps = pd.to_datetime(df["timestamp"])
    close = df["close"].values

    atr = compute_atr(df, period=atr_period).values
    atr = np.nan_to_num(atr, nan=np.nanmean(atr) or 1.0)

    swings: List[Swing] = []
    n = len(df)

    max_conf = left + right

    for i in range(left, n - right):
        is_swing_high = True
        is_swing_low = True

        # Check left and right for swing high
        for j in range(1, left + 1):
            if highs[i - j] >= highs[i]:
                is_swing_high = False
                break
        if is_swing_high:
            for j in range(1, right + 1):
                if highs[i + j] >= highs[i]:
                    is_swing_high = False
                    break

        # Check left and right for swing low
        for j in range(1, left + 1):
            if lows[i - j] <= lows[i]:
                is_swing_low = False
                break
        if is_swing_low:
            for j in range(1, right + 1):
                if lows[i + j] <= lows[i]:
                    is_swing_low = False
                    break

        if not (is_swing_high or is_swing_low):
            continue

        # Calculate strength
        local_atr = atr[i] if i < len(atr) else atr[-1]
        if is_swing_high:
            # Size relative to recent average range
            recent_range = np.mean(highs[max(0, i-20):i+1] - lows[max(0, i-20):i+1])
            rel_size = min(1.0, recent_range / (local_atr * 2.5) if local_atr > 0 else 0.5)
            conf_width = (left + right) / max_conf
            strength = float(np.clip(0.4 * conf_width + 0.6 * rel_size, 0.0, 1.0))

            if strength >= min_strength:
                swings.append(Swing(
                    idx=i,
                    timestamp=timestamps.iloc[i],
                    price=float(highs[i]),
                    swing_type="HIGH",
                    strength=round(strength, 3),
                ))

        if is_swing_low:
            recent_range = np.mean(highs[max(0, i-20):i+1] - lows[max(0, i-20):i+1])
            rel_size = min(1.0, recent_range / (local_atr * 2.5) if local_atr > 0 else 0.5)
            conf_width = (left + right) / max_conf
            strength = float(np.clip(0.4 * conf_width + 0.6 * rel_size, 0.0, 1.0))

            if strength >= min_strength:
                swings.append(Swing(
                    idx=i,
                    timestamp=timestamps.iloc[i],
                    price=float(lows[i]),
                    swing_type="LOW",
                    strength=round(strength, 3),
                ))

    return swings


# =============================================================================
# STRUCTURE LABELING + BOS / CHOCH
# =============================================================================

def detect_structure_breaks(
    swings: List[Swing],
    df: pd.DataFrame,
) -> Tuple[List[StructureBreak], StructureBias]:
    """
    Walk the sequence of confirmed swings and detect:
    - BOS (Break of Structure): Continuation in current bias direction
    - CHOCH (Change of Character): Potential reversal of market structure

    Returns breaks (most recent last) and current bias.
    """
    if len(swings) < 3:
        return [], "NEUTRAL"

    breaks: List[StructureBreak] = []
    bias: StructureBias = "NEUTRAL"

    # Sort by time (they should already be in order)
    sorted_swings = sorted(swings, key=lambda s: s.idx)

    # Track last significant high and low
    last_high = None
    last_low = None

    prev_swing = sorted_swings[0]

    for s in sorted_swings[1:]:
        if s.swing_type == "HIGH":
            if last_high is not None and s.price > last_high.price:
                # Potential bullish break
                if bias in ("BULLISH", "NEUTRAL"):
                    breaks.append(StructureBreak(
                        idx=s.idx,
                        timestamp=s.timestamp,
                        break_type="BOS",
                        direction="BULL",
                        broken_price=last_high.price,
                        confirming_price=s.price,
                    ))
                    bias = "BULLISH"
                else:
                    # Was bearish, broke prior high → CHOCH bullish
                    breaks.append(StructureBreak(
                        idx=s.idx,
                        timestamp=s.timestamp,
                        break_type="CHOCH",
                        direction="BULL",
                        broken_price=last_high.price,
                        confirming_price=s.price,
                    ))
                    bias = "BULLISH"
            last_high = s

        elif s.swing_type == "LOW":
            if last_low is not None and s.price < last_low.price:
                if bias in ("BEARISH", "NEUTRAL"):
                    breaks.append(StructureBreak(
                        idx=s.idx,
                        timestamp=s.timestamp,
                        break_type="BOS",
                        direction="BEAR",
                        broken_price=last_low.price,
                        confirming_price=s.price,
                    ))
                    bias = "BEARISH"
                else:
                    breaks.append(StructureBreak(
                        idx=s.idx,
                        timestamp=s.timestamp,
                        break_type="CHOCH",
                        direction="BEAR",
                        broken_price=last_low.price,
                        confirming_price=s.price,
                    ))
                    bias = "BEARISH"
            last_low = s

    # If no breaks detected yet, infer from last two swings
    if not breaks and len(sorted_swings) >= 2:
        recent = sorted_swings[-2:]
        if recent[0].swing_type == "LOW" and recent[1].swing_type == "HIGH":
            bias = "BULLISH"
        elif recent[0].swing_type == "HIGH" and recent[1].swing_type == "LOW":
            bias = "BEARISH"

    return breaks, bias


# =============================================================================
# ORDER BLOCKS (The real "support/resistance" in SMC)
# =============================================================================

def identify_order_blocks(
    df: pd.DataFrame,
    swings: List[Swing],
    breaks: List[StructureBreak],
    atr: pd.Series,
    displacement_mult: float = 1.8,
    lookback: int = 60,
) -> List[OrderBlock]:
    """
    Identify bullish and bearish order blocks.

    Bullish OB:
      - Last bearish (or small body) candle immediately before a strong bullish
        displacement that caused a BOS or significant upward move.

    Bearish OB is the inverse.

    We only keep relatively recent blocks and mark mitigation.
    """
    if len(df) < 10:
        return []

    obs: List[OrderBlock] = []
    n = len(df)
    atr_vals = atr.values

    # Index recent breaks for quick lookup
    break_indices = {b.idx for b in breaks}

    # Simple recent lookback to avoid ancient OBs
    start_idx = max(0, n - lookback)

    for i in range(start_idx + 2, n - 1):
        # Bullish OB candidate: candle before a strong up move
        curr = df.iloc[i]
        prev = df.iloc[i-1]

        body = abs(curr.close - curr.open)
        candle_range = curr.high - curr.low
        if candle_range == 0:
            continue

        close_pos = (curr.close - curr.low) / candle_range

        # Strong bullish displacement into new highs or after a break
        is_strong_bull = (
            curr.close > prev.close and
            close_pos > 0.7 and
            candle_range > (atr_vals[i] * displacement_mult)
        )

        if is_strong_bull and (i in break_indices or curr.high > df["high"].iloc[i-5:i].max()):
            # The order block is typically the last opposing candle(s)
            ob_candle = df.iloc[i-1]
            ob_high = max(ob_candle.open, ob_candle.close)
            ob_low = min(ob_candle.open, ob_candle.close)

            # Find nearest prior swing low as origin reference
            origin = None
            for sw in reversed(swings):
                if sw.idx < i and sw.swing_type == "LOW":
                    origin = sw.idx
                    break

            disp_size = candle_range / atr_vals[i] if atr_vals[i] > 0 else 1.0

            obs.append(OrderBlock(
                idx=i-1,
                timestamp=pd.to_datetime(ob_candle.name) if hasattr(ob_candle, 'name') else df.index[i-1],
                ob_type="BULLISH",
                high=float(ob_high),
                low=float(ob_low),
                origin_swing_idx=origin,
                displacement_size=round(disp_size, 2),
                strength=min(1.0, disp_size / 3.0),
            ))

        # Bearish OB candidate
        is_strong_bear = (
            curr.close < prev.close and
            close_pos < 0.3 and
            candle_range > (atr_vals[i] * displacement_mult)
        )

        if is_strong_bear and (i in break_indices or curr.low < df["low"].iloc[i-5:i].min()):
            ob_candle = df.iloc[i-1]
            ob_high = max(ob_candle.open, ob_candle.close)
            ob_low = min(ob_candle.open, ob_candle.close)

            origin = None
            for sw in reversed(swings):
                if sw.idx < i and sw.swing_type == "HIGH":
                    origin = sw.idx
                    break

            disp_size = candle_range / atr_vals[i] if atr_vals[i] > 0 else 1.0

            obs.append(OrderBlock(
                idx=i-1,
                timestamp=pd.to_datetime(ob_candle.name) if hasattr(ob_candle, 'name') else df.index[i-1],
                ob_type="BEARISH",
                high=float(ob_high),
                low=float(ob_low),
                origin_swing_idx=origin,
                displacement_size=round(disp_size, 2),
                strength=min(1.0, disp_size / 3.0),
            ))

    # Mark mitigation for existing OBs (price has returned into the block)
    for ob in obs:
        for j in range(ob.idx + 1, min(ob.idx + 40, n)):
            bar = df.iloc[j]
            if ob.ob_type == "BULLISH":
                if bar.low <= ob.high and bar.high >= ob.low:
                    ob.is_mitigated = True
                    ob.mitigation_idx = j
                    break
            else:
                if bar.high >= ob.low and bar.low <= ob.high:
                    ob.is_mitigated = True
                    ob.mitigation_idx = j
                    break

    # Return only the strongest / most recent non-mitigated first
    obs.sort(key=lambda o: (not o.is_mitigated, o.strength, o.idx), reverse=True)
    return obs[:12]  # cap for practicality


# =============================================================================
# LIQUIDITY & FVGs
# =============================================================================

def find_liquidity_levels(
    swings: List[Swing],
    atr: float,
    tolerance_mult: float = 0.25,
) -> List[LiquidityLevel]:
    """Find clusters of equal highs or lows (liquidity pools / stop runs)."""
    if not swings or atr <= 0:
        return []

    levels: List[LiquidityLevel] = []
    tolerance = atr * tolerance_mult

    # Group highs
    highs = [s for s in swings if s.swing_type == "HIGH"]
    for i, h in enumerate(highs):
        cluster = [h]
        for h2 in highs[i+1:]:
            if abs(h2.price - h.price) <= tolerance:
                cluster.append(h2)
        if len(cluster) >= 2:
            avg_price = float(np.mean([c.price for c in cluster]))
            levels.append(LiquidityLevel(
                price=round(avg_price, 5),
                level_type="EQUAL_HIGHS",
                count=len(cluster),
                strength=min(1.0, len(cluster) / 5.0),
                last_touched_idx=max(c.idx for c in cluster),
            ))

    # Group lows
    lows = [s for s in swings if s.swing_type == "LOW"]
    for i, l in enumerate(lows):
        cluster = [l]
        for l2 in lows[i+1:]:
            if abs(l2.price - l.price) <= tolerance:
                cluster.append(l2)
        if len(cluster) >= 2:
            avg_price = float(np.mean([c.price for c in cluster]))
            levels.append(LiquidityLevel(
                price=round(avg_price, 5),
                level_type="EQUAL_LOWS",
                count=len(cluster),
                strength=min(1.0, len(cluster) / 5.0),
                last_touched_idx=max(c.idx for c in cluster),
            ))

    # Dedup by price proximity
    deduped = []
    for lvl in sorted(levels, key=lambda x: -x.strength):
        if not any(abs(lvl.price - d.price) < tolerance * 0.6 for d in deduped):
            deduped.append(lvl)
    return deduped[:8]


def find_fvgs(
    df: pd.DataFrame,
    atr: pd.Series,
    min_size_mult: float = 0.35,
) -> List[FairValueGap]:
    """Detect unfilled fair value gaps (imbalances)."""
    fvgs: List[FairValueGap] = []
    n = len(df)

    for i in range(1, n - 1):
        prev = df.iloc[i-1]
        curr = df.iloc[i]

        # Bullish FVG: current low > prev high (gap between)
        if curr.low > prev.high:
            gap_size = curr.low - prev.high
            if gap_size > (atr.iloc[i] * min_size_mult):
                fvgs.append(FairValueGap(
                    idx=i,
                    timestamp=pd.to_datetime(df["timestamp"].iloc[i]),
                    fvg_type="BULLISH",
                    upper=float(curr.low),
                    lower=float(prev.high),
                    size=round(gap_size, 5),
                ))

        # Bearish FVG: current high < prev low
        if curr.high < prev.low:
            gap_size = prev.low - curr.high
            if gap_size > (atr.iloc[i] * min_size_mult):
                fvgs.append(FairValueGap(
                    idx=i,
                    timestamp=pd.to_datetime(df["timestamp"].iloc[i]),
                    fvg_type="BEARISH",
                    upper=float(prev.low),
                    lower=float(curr.high),
                    size=round(gap_size, 5),
                ))

    # Simple fill detection (later price action entered the gap)
    for fvg in fvgs:
        for j in range(fvg.idx + 1, min(fvg.idx + 30, n)):
            bar = df.iloc[j]
            if fvg.fvg_type == "BULLISH":
                if bar.low <= fvg.upper and bar.high >= fvg.lower:
                    fvg.is_filled = True
                    fvg.fill_idx = j
                    break
            else:
                if bar.high >= fvg.lower and bar.low <= fvg.upper:
                    fvg.is_filled = True
                    fvg.fill_idx = j
                    break

    return fvgs[-10:]  # recent only


# =============================================================================
# SESSION / AMD ANALYSIS (Much better than hardcoded hours)
# =============================================================================

def analyze_session_structure(
    df: pd.DataFrame,
    symbol: str,
    atr: pd.Series,
) -> SessionRange:
    """
    Build real session context instead of pure clock bins.

    Supports different instruments:
    - XAUUSD / Forex: Classic Asian (22-07), London (07-11), NY (13:30-17)
    - US Indices (US30, USTEC, etc.): Main session ~13:30-20:00 UTC
    - European Indices (DE30, etc.): European session ~07:00-15:30 UTC

    Still uses dynamic range detection (actual Asian range + manipulation/expansion)
    instead of blindly trusting the clock.
    """
    if len(df) < 20:
        return SessionRange(phase="UNKNOWN")

    recent = df.tail(min(200, len(df))).copy()
    recent["hour"] = pd.to_datetime(recent["timestamp"]).dt.hour

    current_price = float(recent["close"].iloc[-1])
    current_hour = int(recent["hour"].iloc[-1])

    # Determine instrument group
    sym = symbol.upper()
    is_us_index = any(x in sym for x in ["US30", "USTEC", "NAS", "NDX", "SPX", "DJI"])
    is_eu_index = any(x in sym for x in ["DE30", "DAX", "GER", "EU", "ESTX"])

    phase: SessionPhase = "UNKNOWN"
    manipulation = False
    expansion_dir = None

    # --- Define hour buckets per instrument group (UTC assumption) ---
    if is_us_index:
        # US Indices: Overnight low-vol, then main US session
        if 0 <= current_hour < 13:
            phase = "ASIAN"          # Overnight / pre-market (low vol)
        elif 13 <= current_hour < 17:
            phase = "NY_OPEN"        # Main US open / London overlap
        elif 17 <= current_hour < 21:
            phase = "NY_PM"          # Late US session
        else:
            phase = "OFF_SESSION"

        # For US indices we still compute an "Asian" range from the overnight period
        asian_mask = recent["hour"].between(0, 12)
        london_mask = recent["hour"].between(13, 16)   # London overlap with US open
        ny_mask = recent["hour"].between(13, 20)

    elif is_eu_index:
        # European Indices: Main session during European hours
        if 0 <= current_hour < 7:
            phase = "ASIAN"          # Overnight
        elif 7 <= current_hour < 11:
            phase = "LONDON_OPEN"    # European open / London
        elif 11 <= current_hour < 16:
            phase = "NY_OPEN"        # European afternoon + NY open
        else:
            phase = "OFF_SESSION"

        asian_mask = recent["hour"].between(0, 6)
        london_mask = recent["hour"].between(7, 10)
        ny_mask = recent["hour"].between(13, 17)

    else:
        # Default = Gold / Forex logic (original)
        if 0 <= current_hour < 7 or 22 <= current_hour <= 23:
            phase = "ASIAN"
        elif 7 <= current_hour < 11:
            phase = "LONDON_OPEN"
        elif 11 <= current_hour < 13:
            phase = "NY_OPEN"
        elif 13 <= current_hour < 18:
            phase = "NY_PM"
        else:
            phase = "OFF_SESSION"

        asian_mask = recent["hour"].between(22, 23) | recent["hour"].between(0, 6)
        london_mask = recent["hour"].between(7, 10)
        ny_mask = recent["hour"].between(13, 17)

    # Calculate actual ranges from the data (this part is instrument-agnostic and very useful)
    asian = recent[asian_mask]
    london = recent[london_mask]
    ny = recent[ny_mask]

    asian_high = float(asian["high"].max()) if len(asian) > 3 else None
    asian_low = float(asian["low"].min()) if len(asian) > 3 else None
    asian_range = (asian_high - asian_low) if asian_high and asian_low else None

    # Detect manipulation (price breaking the overnight range during "London" equivalent hours)
    if phase in ("LONDON_OPEN", "NY_OPEN") and asian_range and asian_range > 0:
        if current_price > asian_high or current_price < asian_low:
            manipulation = True

    # Detect expansion out of the overnight range
    if phase in ("NY_OPEN", "NY_PM") and asian_range and asian_range > 0:
        if current_price > asian_high * 1.0008:
            expansion_dir = "UP"
        elif current_price < asian_low * 0.9992:
            expansion_dir = "DOWN"

    is_expanded = expansion_dir is not None

    return SessionRange(
        phase=phase,
        asian_high=asian_high,
        asian_low=asian_low,
        asian_range=asian_range,
        is_expanded=is_expanded,
        expansion_direction=expansion_dir,
        manipulation_detected=manipulation,
    )


# =============================================================================
# MAIN ENTRYPOINT
# =============================================================================

def compute_market_structure(
    df: pd.DataFrame,
    symbol: str = "UNKNOWN",
    timeframe: str = "M5",
    swing_left: int = 3,   # Lowered for live data feeder bootstrapping (needs fewer bars to detect swings)
    swing_right: int = 3,  # Lowered for live data feeder bootstrapping (needs fewer bars to detect swings)
    min_candles: int = 15,   # Lowered for live data feeder bootstrapping
    # swing_left/right defaulted to 3 (instead of 5) for earlier detection with limited bars
) -> MarketStructure:
    """
    Primary function to call from signals / strategies.

    Takes a properly ordered OHLCV DataFrame (oldest first) with columns:
    timestamp, open, high, low, close, volume (tick_volume is fine)

    Returns a rich MarketStructure object.
    """
    if len(df) < min_candles:
        if len(df) < 5:
            raise ValueError(f"Need at least 5 candles to run structure analysis (got {len(df)})")
        # Soft warning path for testing — we allow it but it's not ideal
        print(f"[Structure] Warning: Running with only {len(df)} candles (min recommended: {min_candles})")

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    atr_series = compute_atr(df, period=14)
    atr_value = float(atr_series.iloc[-1])

    # 1. Swings
    swings = find_swings(df, left=swing_left, right=swing_right)

    # 2. Breaks + bias
    breaks, bias = detect_structure_breaks(swings, df)

    # 3. Order blocks
    obs = identify_order_blocks(df, swings, breaks, atr_series)

    # 4. Liquidity
    liq = find_liquidity_levels(swings, atr_value)

    # 5. FVGs
    fvgs = find_fvgs(df, atr_series)

    # 6. Session / AMD
    session = analyze_session_structure(df, symbol, atr_series)

    # Recent displacement
    disp_mask = detect_displacement(df, atr_series, min_mult=1.7)
    recent_disp = None
    if disp_mask.any():
        last_disp_idx = int(disp_mask[disp_mask].index[-1])
        recent_disp = {
            "idx": last_disp_idx,
            "direction": "BULL" if df["close"].iloc[last_disp_idx] > df["open"].iloc[last_disp_idx] else "BEAR",
            "size_atr": round((df["high"].iloc[last_disp_idx] - df["low"].iloc[last_disp_idx]) / atr_value, 2),
        }

    ms = MarketStructure(
        symbol=symbol.upper(),
        timeframe=timeframe.upper(),
        last_bar_idx=len(df) - 1,
        last_bar_time=df["timestamp"].iloc[-1],
        swings=swings,
        breaks=breaks,
        bias=bias,
        order_blocks=obs,
        liquidity_levels=liq,
        fvgs=fvgs,
        session=session,
        recent_displacement=recent_disp,
        current_price=float(df["close"].iloc[-1]),
        atr=atr_value,
    )
    return ms


# =============================================================================
# HUMAN READABLE PROGRESS / STATUS SUMMARY (used by realtime responses + UI)
# =============================================================================

def generate_structure_summary(ms: MarketStructure) -> str:
    """
    Produces the compact one-line status string shown in logs and the future UI, e.g.:
    "BULLISH bias | 19 swing(s) | 0 active bullish OB(s), 1 active bearish OB(s) | no unfilled FVGs | NY_OPEN session | Recent: CHOCH BULL at 50973.0, BOS BEAR at 51170.0"
    """
    try:
        bias = getattr(ms, "bias", "NEUTRAL")
        swings = len(getattr(ms, "swings", []))
        obs = getattr(ms, "order_blocks", []) or []
        active_bull = sum(1 for o in obs if getattr(o, "ob_type", "") == "BULLISH" and not getattr(o, "is_mitigated", True))
        active_bear = sum(1 for o in obs if getattr(o, "ob_type", "") == "BEARISH" and not getattr(o, "is_mitigated", True))

        fvgs = getattr(ms, "fvgs", []) or []
        unfilled_bull_fvgs = sum(1 for f in fvgs if getattr(f, "fvg_type", "") == "BULLISH" and not getattr(f, "is_filled", True))
        unfilled_bear_fvgs = sum(1 for f in fvgs if getattr(f, "fvg_type", "") == "BEARISH" and not getattr(f, "is_filled", True))

        liq_count = len(getattr(ms, "liquidity_levels", []) or [])

        session_obj = getattr(ms, "session", None)
        session = getattr(session_obj, "phase", "UNKNOWN") if session_obj else "UNKNOWN"
        manip = getattr(session_obj, "manipulation_detected", False) if session_obj else False

        recent_breaks = (getattr(ms, "breaks", []) or [])[-3:]
        recent_parts = []
        for b in recent_breaks:
            btype = getattr(b, "break_type", "BREAK")
            bdir = getattr(b, "direction", "?")
            bprice = round(getattr(b, "broken_price", getattr(b, "price", 0.0)), 2)
            recent_parts.append(f"{btype} {bdir} at {bprice}")

        recent_str = ", ".join(recent_parts) if recent_parts else "none"

        fvg_str = "no unfilled FVGs" if (unfilled_bull_fvgs + unfilled_bear_fvgs == 0) else f"{unfilled_bull_fvgs} bull / {unfilled_bear_fvgs} bear unfilled FVGs"
        manip_str = " | manipulation" if manip else ""

        return (
            f"{bias} bias | {swings} swing(s) | "
            f"{active_bull} active bullish OB(s), {active_bear} active bearish OB(s) | "
            f"{fvg_str} | {session} session{manip_str} | Recent: {recent_str}"
        ).strip()
    except Exception:
        return f"{getattr(ms, 'bias', 'NEUTRAL')} bias | limited data"


# Convenience: lightweight dict for gradual migration (old-style consumers)
def market_structure_to_legacy_features(ms: MarketStructure, df: pd.DataFrame) -> Dict:
    """
    Bridge function: convert rich structure into a flat dict similar to the old
    compute_features output so existing strategy code can be ported incrementally.
    """
    close = float(df["close"].iloc[-1])
    swing_high = max([s.price for s in ms.swings if s.swing_type == "HIGH"], default=close)
    swing_low = min([s.price for s in ms.swings if s.swing_type == "LOW"], default=close)

    fib_range = swing_high - swing_low
    return {
        "current_close": close,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "fib_618": swing_high - fib_range * 0.618,
        "fib_500": swing_high - fib_range * 0.500,
        "fib_382": swing_high - fib_range * 0.382,
        "atr": ms.atr,
        "session": ms.session.phase.lower() if ms.session.phase != "UNKNOWN" else "off",
        "bias": ms.bias,
        "manipulation_detected": ms.session.manipulation_detected,
        "active_bull_obs": len([o for o in ms.order_blocks if o.ob_type == "BULLISH" and not o.is_mitigated]),
        "active_bear_obs": len([o for o in ms.order_blocks if o.ob_type == "BEARISH" and not o.is_mitigated]),
        "recent_displacement": ms.recent_displacement,
        # Legacy placeholders (will be removed in full migration)
        "ema_9": close,   # deliberately neutral
        "ema_21": close,
        "rsi": 50.0,
        "macd_hist": 0.0,
    }
