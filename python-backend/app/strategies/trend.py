def analyze(f: dict) -> float:
    """
    Trend Following Strategy
    Returns score: +1 (bullish), -1 (bearish), 0 (neutral)
    Uses EMA alignment and price position.
    """
    score = 0.0
    price = f["current_close"]

    # EMA alignment: 9 > 21 > 50 = strong uptrend
    if f["ema_9"] > f["ema_21"] > f["ema_50"]:
        score += 1.0
    elif f["ema_9"] < f["ema_21"] < f["ema_50"]:
        score -= 1.0

    # Price above/below EMA 200 = macro trend
    if price > f["ema_200"]:
        score += 0.5
    elif price < f["ema_200"]:
        score -= 0.5

    # MACD histogram direction
    if f["macd_hist"] > 0 and f["macd"] > f["macd_signal"]:
        score += 0.5
    elif f["macd_hist"] < 0 and f["macd"] < f["macd_signal"]:
        score -= 0.5

    # Normalize to [-1, 1]
    return max(-1.0, min(1.0, score / 2.0))