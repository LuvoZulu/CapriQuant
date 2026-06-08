"""
Structure & Order Block Strategy (New)

This replaces the old trend.py and parts of breakout.py.

It only generates meaningful scores when there is clear market structure
(BOS + active order blocks + session context). Everything else returns near zero.

No MACD, no EMA crosses, no vanilla RSI as primary drivers.
"""

from typing import Dict
from app.features.structure import MarketStructure


def analyze_structure(ms: MarketStructure) -> float:
    """
    Returns a directional score in [-1.0, 1.0] based purely on structure.
    Uses MarketStructure to its FULL potential:
    - BOS/CHOCH alignment + count/quality
    - Active OB count + individual strength + displacement_size
    - Unfilled FVGs in direction (imbalance support)
    - Liquidity levels strength + type
    - Recent displacement magnitude/direction
    - Swing count/quality for structure maturity
    - Session richness (manipulation, expansion, phase) for timing multiplier
    No legacy indicators. Autonomous from MS fields.
    """
    if not ms:
        return 0.0
    score = 0.0
    price = ms.current_price
    atr = ms.atr or 1.0

    # 1. Breaks (BOS/CHOCH) — quality + count + recency
    bull_bos = [b for b in ms.breaks if b.break_type == "BOS" and b.direction == "BULL"]
    bear_bos = [b for b in ms.breaks if b.break_type == "BOS" and b.direction == "BEAR"]
    recent_choch_against = [b for b in ms.breaks[-3:] if b.break_type == "CHOCH" and
                            ((ms.bias == "BULLISH" and b.direction == "BEAR") or (ms.bias == "BEARISH" and b.direction == "BULL"))]

    if ms.bias == "BULLISH":
        score += min(0.8, len(bull_bos) * 0.22)
        if bull_bos:
            # prefer recent
            score += 0.15 if bull_bos[-1] in ms.breaks[-3:] else 0.0
    elif ms.bias == "BEARISH":
        score -= min(0.8, len(bear_bos) * 0.22)
        if bear_bos:
            score -= 0.15 if bear_bos[-1] in ms.breaks[-3:] else 0.0

    # Strong penalty for recent opposing CHOCH (structure broken)
    if recent_choch_against:
        score *= (0.4 if len(recent_choch_against) >= 1 else 0.7)

    # 2. Active OBs with full richness (strength + displacement_size)
    active_bull = [ob for ob in ms.order_blocks if ob.ob_type == "BULLISH" and not ob.is_mitigated]
    active_bear = [ob for ob in ms.order_blocks if ob.ob_type == "BEARISH" and not ob.is_mitigated]

    for ob in active_bull:
        dist = abs(price - ob.high)
        if dist < atr * 1.1:
            ob_contrib = 0.38 * getattr(ob, 'strength', 1.0)
            ob_contrib += min(0.25, getattr(ob, 'displacement_size', 0.0) * 0.12)  # bigger impulse OB = stronger
            score += ob_contrib

    for ob in active_bear:
        dist = abs(price - ob.low)
        if dist < atr * 1.1:
            ob_contrib = 0.38 * getattr(ob, 'strength', 1.0)
            ob_contrib += min(0.25, getattr(ob, 'displacement_size', 0.0) * 0.12)
            score -= ob_contrib

    # 3. Unfilled FVGs in bias direction (imbalance as fuel)
    unfilled_bull_fvg = len([f for f in ms.fvgs if f.fvg_type == "BULLISH" and not f.is_filled])
    unfilled_bear_fvg = len([f for f in ms.fvgs if f.fvg_type == "BEARISH" and not f.is_filled])
    if ms.bias == "BULLISH":
        score += min(0.35, unfilled_bull_fvg * 0.12)
    elif ms.bias == "BEARISH":
        score -= min(0.35, unfilled_bear_fvg * 0.12)

    # 4. Liquidity support (sweeps or clusters near)
    liq_support = 0.0
    for liq in ms.liquidity_levels:
        d = abs(price - liq.price)
        if d < atr * 0.9:
            liq_support += getattr(liq, 'strength', 0.6) * 0.18
    if ms.bias == "BULLISH":
        score += min(0.3, liq_support)
    else:
        score -= min(0.3, liq_support)

    # 5. Recent displacement confirmation
    if ms.recent_displacement:
        disp_dir = ms.recent_displacement.get("direction")
        disp_size = ms.recent_displacement.get("size", 0.0)  # assume normalized
        if disp_dir == "BULL" and ms.bias == "BULLISH":
            score += min(0.28, disp_size * 0.18)
        elif disp_dir == "BEAR" and ms.bias == "BEARISH":
            score -= min(0.28, disp_size * 0.18)

    # 6. Swing maturity (more swings = more established structure)
    swing_count = len(ms.swings)
    if swing_count >= 5:
        maturity = min(0.22, (swing_count - 4) * 0.04)
        score = score + maturity if ms.bias == "BULLISH" else score - maturity

    # 7. Session richness multipliers (from MS.session)
    sess = ms.session
    if getattr(sess, 'manipulation_detected', False):
        # Manipulation often precedes real move — temper or flip caution
        score *= 0.72
    if getattr(sess, 'phase', '') in ("NY_OPEN", "NY_PM") and getattr(sess, 'is_expanded', False):
        exp = getattr(sess, 'expansion_direction', None)
        if (ms.bias == "BULLISH" and exp == "UP") or (ms.bias == "BEARISH" and exp == "DOWN"):
            score *= 1.22   # strong expansion confirmation
        else:
            score *= 1.08

    return max(-1.0, min(1.0, score))


def analyze(ms: Dict) -> float:
    """
    Backward-compatible thin wrapper.
    Expects either a full 'market_structure' object or the legacy dict.
    """
    if "market_structure" in ms and isinstance(ms["market_structure"], dict):
        # In real use we would reconstruct, but for now return neutral
        # until full migration of the old path.
        return 0.0
    return 0.0
