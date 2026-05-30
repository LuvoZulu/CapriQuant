"""
Real AMD (Accumulation-Manipulation-Distribution) Strategy
Contextual version using proper MarketStructure.

This is the heart of smart-money timing:
- Asian session = Accumulation (range building, liquidity)
- London Open = Manipulation (stop hunts / inducement of the Asian range)
- NY session = Distribution / real directional expansion

Only produces strong signals when price behavior matches the expected session dynamics.
No reliance on EMA crosses or vanilla RSI as primary drivers.
"""

from typing import Dict
from app.features.structure import MarketStructure


def analyze_amd_structure(ms: MarketStructure) -> float:
    """
    Core contextual AMD analyzer.
    Returns strong directional score only when session behavior + structure align.
    """
    score = 0.0
    price = ms.current_price
    atr = ms.atr or (price * 0.0008)
    session = ms.session

    # --- ACCUMULATION (Asian) ---
    if session.phase in ("ASIAN", "OFF_SESSION"):
        # During accumulation we mostly wait or prepare for manipulation.
        # Very small score unless we see extreme contraction (very tight range)
        if session.asian_range and session.asian_range < atr * 1.2:
            score = 0.15 if ms.bias == "BULLISH" else -0.15   # slight bias toward expansion direction later
        return max(-0.3, min(0.3, score))   # deliberately low conviction

    # --- MANIPULATION (London Open / early NY) - Made stricter ---
    if session.phase == "LONDON_OPEN" or session.manipulation_detected:
        swept_high = session.asian_high and price >= session.asian_high - (atr * 0.08)
        swept_low  = session.asian_low  and price <= session.asian_low  + (atr * 0.08)

        # Require displacement confirmation for high scores
        has_bull_disp = ms.recent_displacement and ms.recent_displacement.get("direction") == "BULL"
        has_bear_disp = ms.recent_displacement and ms.recent_displacement.get("direction") == "BEAR"

        if swept_low and ms.bias in ("BULLISH", "NEUTRAL"):
            score += 0.85
            if has_bull_disp:
                score += 0.45   # Much higher reward for confirmed displacement

        elif swept_high and ms.bias in ("BEARISH", "NEUTRAL"):
            score -= 0.85
            if has_bear_disp:
                score -= 0.45

        # Without displacement after sweep, be very conservative
        if (swept_low or swept_high) and not (has_bull_disp or has_bear_disp):
            score *= 0.35

        return max(-1.0, min(1.0, score))

    # --- DISTRIBUTION / EXPANSION (NY session) - more forgiving for trends ---
    if session.phase in ("NY_OPEN", "NY_PM"):
        if session.is_expanded:
            if ms.bias == "BULLISH" and session.expansion_direction == "UP":
                score += 0.75
                active_bull_obs = len([ob for ob in ms.order_blocks if ob.ob_type == "BULLISH" and not ob.is_mitigated])
                if active_bull_obs > 0:
                    score += 0.2
                recent_bos = any(b.break_type == "BOS" and b.direction == "BULL" for b in ms.breaks[-2:])
                if recent_bos:
                    score += 0.15

            elif ms.bias == "BEARISH" and session.expansion_direction == "DOWN":
                score -= 0.75
                active_bear_obs = len([ob for ob in ms.order_blocks if ob.ob_type == "BEARISH" and not ob.is_mitigated])
                if active_bear_obs > 0:
                    score -= 0.2
                recent_bos = any(b.break_type == "BOS" and b.direction == "BEAR" for b in ms.breaks[-2:])
                if recent_bos:
                    score -= 0.15

        # In strong bias, still give credit even without perfect expansion
        elif ms.bias == "BULLISH":
            score += 0.35
        elif ms.bias == "BEARISH":
            score -= 0.35

        return max(-1.0, min(1.0, score))

    return 0.0


def analyze(f: dict) -> float:
    """
    Backward-compatible wrapper for legacy path.
    When the new structure engine is used, this is largely bypassed.
    """
    # Legacy path still uses old crude features — deliberately return muted signal
    # so it doesn't fight the new engine.
    if f.get("structure_engine"):
        return 0.0
    return 0.0   # old path is intentionally de-emphasized now