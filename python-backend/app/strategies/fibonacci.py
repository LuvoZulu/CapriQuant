"""
Fibonacci Confluence Strategy (Contextual)

Fibonacci levels ONLY have edge when they land on other high-quality structure:
- Order Blocks
- Liquidity pools / previous swings
- Fair Value Gaps
- After displacement / BOS

This version completely ignores standalone "price near 0.618" signals.
It requires confluence with actual market structure.
"""

from typing import Dict
from app.features.structure import MarketStructure


def analyze_fib_confluence(ms: MarketStructure) -> float:
    """
    Returns a high score only when a Fibonacci retracement or extension
    lands inside or very close to a meaningful structural level (OB, liquidity, FVG).
    """
    if ms.bias == "NEUTRAL" or len(ms.swings) < 3:
        return 0.0

    price = ms.current_price
    atr = ms.atr or (price * 0.0008)
    zone = atr * 0.42   # reasonable confluence zone

    score = 0.0

    # Use the most recent significant impulse leg from swings
    sorted_swings = sorted(ms.swings, key=lambda s: s.idx)
    if len(sorted_swings) < 2:
        return 0.0

    # Find last major swing low to high (or high to low) that created displacement/BOS
    recent_highs = [s for s in sorted_swings if s.swing_type == "HIGH"][-2:]
    recent_lows  = [s for s in sorted_swings if s.swing_type == "LOW"][-2:]

    fib_levels = []

    if len(recent_highs) >= 1 and len(recent_lows) >= 1:
        # Bullish leg (low to high) → look for buy at retracements
        if ms.bias == "BULLISH":
            leg_high = recent_highs[-1].price
            leg_low = recent_lows[-1].price
            leg = leg_high - leg_low

            fib_levels = [
                ("618", leg_high - leg * 0.618),
                ("500", leg_high - leg * 0.500),
                ("786", leg_high - leg * 0.786),
                ("382", leg_high - leg * 0.382),
            ]

            for name, level in fib_levels:
                if abs(price - level) < zone and name in ("618", "786"):   # Only golden zone now
                    # Check confluence with bullish order blocks
                    bull_obs = [ob for ob in ms.order_blocks if ob.ob_type == "BULLISH" and not ob.is_mitigated]
                    for ob in bull_obs:
                        if ob.low - zone <= level <= ob.high + zone:
                            score += 0.95   # Higher bar, higher reward
                            break
                    else:
                        # Check confluence with unfilled bullish FVG
                        bull_fvgs = [f for f in ms.fvgs if f.fvg_type == "BULLISH" and not f.is_filled]
                        for fvg in bull_fvgs:
                            if fvg.lower - zone <= level <= fvg.upper + zone:
                                score += 0.78
                                break
                        else:
                            # Liquidity confluence
                            for liq in ms.liquidity_levels:
                                if abs(liq.price - level) < zone * 0.6:
                                    score += 0.48
                                    break

        # Bearish leg (high to low) → look for sell at retracements
        elif ms.bias == "BEARISH":
            leg_high = recent_highs[-1].price
            leg_low = recent_lows[-1].price
            leg = leg_high - leg_low

            fib_levels = [
                ("618", leg_low + leg * 0.618),
                ("500", leg_low + leg * 0.500),
                ("786", leg_low + leg * 0.786),
                ("382", leg_low + leg * 0.382),
            ]

            for name, level in fib_levels:
                if abs(price - level) < zone:
                    bear_obs = [ob for ob in ms.order_blocks if ob.ob_type == "BEARISH" and not ob.is_mitigated]
                    for ob in bear_obs:
                        if ob.low - zone <= level <= ob.high + zone:
                            score += 0.92 if name in ("618", "500") else 0.65
                            break
                    else:
                        bear_fvgs = [f for f in ms.fvgs if f.fvg_type == "BEARISH" and not f.is_filled]
                        for fvg in bear_fvgs:
                            if fvg.lower - zone <= level <= fvg.upper + zone:
                                score += 0.75 if name in ("618", "500") else 0.55
                                break
                        else:
                            for liq in ms.liquidity_levels:
                                if abs(liq.price - level) < zone * 0.7:
                                    score += 0.55
                                    break

    # Cap and return
    return max(-1.0, min(1.0, score))


def analyze(f: dict) -> float:
    """Legacy wrapper — muted on new engine path."""
    if f.get("structure_engine"):
        return 0.0
    return 0.0