"""
Contextual Price Action Strategy

Price action patterns (engulfing, pin bars, displacement, rejection) only score highly
when they occur at high-probability structural locations:
- Order Blocks
- Liquidity sweeps
- Fibonacci confluence zones
- After BOS in the direction of the pattern

Standalone candle patterns anywhere on the chart are noise and get near-zero score.
"""

from typing import Dict
from app.features.structure import MarketStructure


def analyze_price_action_contextual(ms: MarketStructure) -> float:
    """
    Scores price action only when it appears at structural confluences.
    """
    if len(ms.swings) < 2:
        return 0.0

    score = 0.0
    price = ms.current_price
    atr = ms.atr or (price * 0.0008)

    # We need the actual last few candles for pattern detection.
    # Since the MarketStructure snapshot doesn't carry the full recent df,
    # we use the recent_displacement + session + proximity to structure as proxy.
    # In a full integration the caller would also pass the last 5-10 candles.

    # 1. Strong displacement after structure (best PA)
    if ms.recent_displacement:
        disp = ms.recent_displacement
        disp_size = disp.get("size_atr", 0)

        if disp["direction"] == "BULL":
            # Bullish displacement near bullish OB or after bullish BOS
            near_bull_ob = any(
                not ob.is_mitigated and ob.ob_type == "BULLISH" and
                abs(price - ob.high) < atr * 0.9
                for ob in ms.order_blocks
            )
            recent_bull_bos = any(
                b.break_type in ("BOS", "CHOCH") and b.direction == "BULL"
                for b in ms.breaks[-2:]
            )
            if near_bull_ob or recent_bull_bos:
                score += 0.95 + min(0.4, (disp_size - 1.5) * 0.25)

        elif disp["direction"] == "BEAR":
            near_bear_ob = any(
                not ob.is_mitigated and ob.ob_type == "BEARISH" and
                abs(price - ob.low) < atr * 0.9
                for ob in ms.order_blocks
            )
            recent_bear_bos = any(
                b.break_type in ("BOS", "CHOCH") and b.direction == "BEAR"
                for b in ms.breaks[-2:]
            )
            if near_bear_ob or recent_bear_bos:
                score -= 0.95 + min(0.4, (disp_size - 1.5) * 0.25)

    # 2. Liquidity sweep + rejection (very high quality PA)
    if ms.session.manipulation_detected:
        for liq in ms.liquidity_levels:
            if liq.level_type == "EQUAL_HIGHS" and price >= liq.price - atr * 0.2:
                # Rejection at equal highs liquidity after sweep
                score -= 0.80
            if liq.level_type == "EQUAL_LOWS" and price <= liq.price + atr * 0.2:
                score += 0.80

    # 3. Basic momentum continuation at structure (weaker but valid)
    if abs(score) < 0.4:
        # Only give small continuation score if we have clear bias + active OB in same direction
        active_bull = any(ob.ob_type == "BULLISH" and not ob.is_mitigated for ob in ms.order_blocks)
        active_bear = any(ob.ob_type == "BEARISH" and not ob.is_mitigated for ob in ms.order_blocks)

        if ms.bias == "BULLISH" and active_bull:
            score += 0.35
        elif ms.bias == "BEARISH" and active_bear:
            score -= 0.35

    # Cap extreme scores
    return max(-1.0, min(1.0, score))


def analyze(f: dict) -> float:
    """Legacy wrapper — muted."""
    if f.get("structure_engine"):
        return 0.0
    return 0.0