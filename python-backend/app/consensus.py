from app.strategies import trend, breakout, amd, fibonacci, price_action, scalping

# =============================================================================
# DEPRECATED — DO NOT USE FOR LIVE TRADING
#
# This module is the old lagging-indicator-heavy consensus (MACD, EMA crosses, RSI).
# It is kept only for historical comparison during the migration.
#
# ALL NEW WORK USES:
#   from app.engine.confluence import get_structure_signal
#   from app.features.structure import compute_market_structure
#
# The old system had no real edge for your goals.
# =============================================================================

# Custom weights — aggressive small account growth focus
WEIGHTS = {
    "scalping":     0.25,
    "breakout":     0.20,
    "price_action": 0.20,
    "amd":          0.15,
    "trend":        0.10,
    "fibonacci":    0.10,
}

# Threshold to trigger a BUY or SELL signal
BUY_THRESHOLD  =  0.35
SELL_THRESHOLD = -0.35


def get_signal(features: dict, spread: float = 0.0) -> dict:
    """
    Runs all strategy modules, applies weights, returns final signal.
    """
    scores = {
        "trend":        trend.analyze(features),
        "breakout":     breakout.analyze(features),
        "amd":          amd.analyze(features),
        "fibonacci":    fibonacci.analyze(features),
        "price_action": price_action.analyze(features),
        "scalping":     scalping.analyze(features, spread),
    }

    # Weighted consensus score
    weighted_score = sum(scores[k] * WEIGHTS[k] for k in scores)

    # Final signal decision
    if weighted_score >= BUY_THRESHOLD:
        signal = "BUY"
    elif weighted_score <= SELL_THRESHOLD:
        signal = "SELL"
    else:
        signal = "HOLD"

    # Confidence: how far from 0 (0% = neutral, 100% = max conviction)
    confidence = round(min(abs(weighted_score) / 0.5 * 100, 100), 1)

    return {
        "signal":     signal,
        "score":      round(weighted_score, 4),
        "confidence": confidence,
        "breakdown":  {k: round(v, 4) for k, v in scores.items()},
        "session":    features.get("session", "unknown"),
        "rsi":        round(features.get("rsi", 0), 2),
        "atr":        round(features.get("atr", 0), 5),
    }