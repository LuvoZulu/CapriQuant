def analyze(f: dict) -> float:
    """
    Breakout Trading Strategy
    Returns score: +1 (bullish breakout), -1 (bearish breakout), 0 (no breakout)
    Confirms with volume.
    """
    score = 0.0
    price  = f["current_close"]
    buffer = f["atr"] * 0.1  # small buffer to avoid false breaks

    volume_confirmed = f["cur_volume"] > f["avg_volume"] * 1.2

    # Bullish breakout above range high
    if price > f["range_high"] - buffer:
        score += 1.0
        if volume_confirmed:
            score += 0.5  # stronger signal with volume

    # Bearish breakout below range low
    elif price < f["range_low"] + buffer:
        score -= 1.0
        if volume_confirmed:
            score -= 0.5

    # Price inside range = no signal
    return max(-1.0, min(1.0, score))