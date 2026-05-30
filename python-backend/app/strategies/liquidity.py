"""
Liquidity Sweep & Inducement Strategy

Detects and trades the classic "stop hunt" behavior at equal highs/lows and session extremes.
This is one of the highest probability components of the AMD model.

Only scores when there is clear evidence of a sweep + rejection or displacement away from the liquidity.
"""

from typing import Dict
from app.features.structure import MarketStructure


def analyze_liquidity_sweeps(ms: MarketStructure) -> float:
    """
    Returns strong directional score when price has swept liquidity
    (equal highs/lows or Asian range extremes) and then shows reversal behavior.
    """
    if not ms.liquidity_levels and not ms.session.asian_high:
        return 0.0

    score = 0.0
    price = ms.current_price
    atr = ms.atr or (price * 0.0008)

    # Equal highs / lows liquidity
    for liq in ms.liquidity_levels:
        dist = abs(price - liq.price)

        if liq.level_type == "EQUAL_HIGHS":
            if dist < atr * 0.18:   # price is at or just through the liquidity
                # Look for rejection signs via recent displacement or session manipulation
                if ms.session.manipulation_detected or (ms.recent_displacement and ms.recent_displacement["direction"] == "BEAR"):
                    score -= 0.90 + (liq.strength * 0.25)
                elif ms.recent_displacement and ms.recent_displacement["direction"] == "BULL":
                    # False sweep then continuation higher (less common but valid)
                    score += 0.35

        elif liq.level_type == "EQUAL_LOWS":
            if dist < atr * 0.18:
                if ms.session.manipulation_detected or (ms.recent_displacement and ms.recent_displacement["direction"] == "BULL"):
                    score += 0.90 + (liq.strength * 0.25)
                elif ms.recent_displacement and ms.recent_displacement["direction"] == "BEAR":
                    score -= 0.35

    # Asian range liquidity sweeps (core of real AMD)
    if ms.session.asian_high and ms.session.asian_low:
        swept_high = price >= ms.session.asian_high - (atr * 0.08)
        swept_low  = price <= ms.session.asian_low  + (atr * 0.08)

        if swept_high and ms.session.phase in ("LONDON_OPEN", "NY_OPEN"):
            # Stop hunt above Asian highs during manipulation window
            if ms.recent_displacement and ms.recent_displacement["direction"] == "BEAR":
                score -= 0.85
            else:
                score -= 0.45   # still bearish bias but lower conviction

        if swept_low and ms.session.phase in ("LONDON_OPEN", "NY_OPEN"):
            if ms.recent_displacement and ms.recent_displacement["direction"] == "BULL":
                score += 0.85
            else:
                score += 0.45

    return max(-1.0, min(1.0, score))


def analyze(f: dict) -> float:
    if f.get("structure_engine"):
        return 0.0
    return 0.0
