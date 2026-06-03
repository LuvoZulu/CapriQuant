"""
CRT (Consolidation Range Theory / Range Confluence) Strategy for CapriQuant.

Provides an additional confluence score for ranges after displacement (manipulation/expansion).

Returns a signed score [-1.0 .. +1.0] that is added to total confluence when at structural locations.

This was explicitly requested for full integration.
"""

from typing import Dict, Any, Optional
import pandas as pd

def analyze_crt_range_confluence(
    df: pd.DataFrame,
    market_structure: Any,
    recent_displacement: float = 0.0,
    bias: str = "NEUTRAL",
    atr: float = 0.0,
) -> float:
    """
    Compute CRT-style range confluence at current price.

    Looks for:
    - Price inside a recent displacement range after BOS/CHOCH
    - Proximity to OB inside the range
    - Expansion after manipulation phase (AMD)
    - Liquidity + range confluence
    - Bias credit

    Returns signed score (positive bullish CRT setup, negative bearish).
    """
    if df is None or len(df) < 8 or market_structure is None:
        return 0.0

    price = float(getattr(market_structure, "current_price", df["close"].iloc[-1]))
    atr = atr or max(0.0001, price * 0.001)

    # Simple recent range (last 8-20 bars)
    recent = df.tail(20)
    rng_high = recent["high"].max()
    rng_low = recent["low"].min()
    rng_mid = (rng_high + rng_low) / 2
    in_range = rng_low <= price <= rng_high

    if not in_range:
        return 0.0

    score = 0.0

    # 1. Price in range after displacement (core CRT)
    disp = abs(recent_displacement) or (rng_high - rng_low)
    if disp > 1.2 * atr:
        score += 0.25 if price > rng_mid else -0.25

    # 2. Proximity to active OB inside range (strong)
    obs = getattr(market_structure, "order_blocks", []) or []
    active_obs = [o for o in obs if not getattr(o, "is_mitigated", True)]
    for ob in active_obs:
        ob_mid = (getattr(ob, "low", price) + getattr(ob, "high", price)) / 2
        if abs(price - ob_mid) < 0.6 * atr and rng_low < ob_mid < rng_high:
            score += 0.35 if getattr(ob, "ob_type", "") == "BULLISH" else -0.35
            break

    # 3. AMD expansion credit (if session or recent bars show expansion after range)
    # Simplified: if last 3 bars have larger range than previous average
    if len(recent) > 5:
        recent_rng = (recent["high"] - recent["low"]).tail(3).mean()
        prev_rng = (recent["high"] - recent["low"]).head(5).mean()
        if recent_rng > prev_rng * 1.15:
            score += 0.2 if bias == "BULLISH" else -0.2

    # 4. Liquidity sweep + retest inside range
    # If recent wick took liquidity (high/low extreme) then returned into range
    liq_sweep = (recent["high"].iloc[-1] > rng_high * 0.998) or (recent["low"].iloc[-1] < rng_low * 1.002)
    if liq_sweep and in_range:
        score += 0.15 if bias == "BULLISH" else -0.15

    # 5. Bias alignment
    if bias == "BULLISH":
        score += 0.1
    elif bias == "BEARISH":
        score -= 0.1

    # Clamp
    return max(-1.0, min(1.0, score))

# Legacy stub for compatibility
def analyze_crt_range_confluence_legacy(df: pd.DataFrame, market_structure: Any = None, **kwargs) -> float:
    return analyze_crt_range_confluence(df, market_structure, **kwargs)
