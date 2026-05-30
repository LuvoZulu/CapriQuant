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
    High scores only when multiple structural elements align.
    """
    score = 0.0

    # Recent BOS in same direction as bias = strong continuation
    recent_bos = [b for b in ms.breaks[-3:] if b.break_type == "BOS"]
    if recent_bos:
        last_bos = recent_bos[-1]
        if last_bos.direction == "BULL" and ms.bias == "BULLISH":
            score += 0.65
        elif last_bos.direction == "BEAR" and ms.bias == "BEARISH":
            score -= 0.65

    # Active order blocks near price
    active_bull = [ob for ob in ms.order_blocks if ob.ob_type == "BULLISH" and not ob.is_mitigated]
    active_bear = [ob for ob in ms.order_blocks if ob.ob_type == "BEARISH" and not ob.is_mitigated]

    for ob in active_bull:
        dist = abs(ms.current_price - ob.high)
        if dist < ms.atr * 0.8:
            score += 0.45 * ob.strength

    for ob in active_bear:
        dist = abs(ms.current_price - ob.low)
        if dist < ms.atr * 0.8:
            score -= 0.45 * ob.strength

    # Session timing filter (AMD)
    if ms.session.manipulation_detected:
        # During manipulation we want to be cautious or counter
        if ms.bias == "BULLISH":
            score *= 0.6   # reduce long bias during potential stop hunt
        elif ms.bias == "BEARISH":
            score *= 0.6

    if ms.session.phase in ("NY_OPEN", "NY_PM") and ms.session.is_expanded:
        # Real move likely happening — amplify structure signal
        score *= 1.15

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
