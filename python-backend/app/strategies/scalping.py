def analyze(f: dict, spread: float = 0.0) -> float:
    """
    Scalping Filter Strategy
    Fast momentum + spread/volatility filter for small account compounding.
    Returns score: +1 (scalp buy), -1 (scalp sell), 0 (skip)
    """
    score = 0.0
    price = f["current_close"]
    atr   = f["atr"]

    # Spread filter — skip if spread is too wide vs ATR
    max_spread = atr * 0.15
    if spread > max_spread:
        return 0.0  # not worth scalping

    # EMA 9/21 cross momentum
    if f["ema_9"] > f["ema_21"]:
        score += 0.8
    elif f["ema_9"] < f["ema_21"]:
        score -= 0.8

    # RSI momentum filter (avoid extremes for scalping)
    rsi = f["rsi"]
    if 45 < rsi < 65 and score > 0:
        score += 0.4   # RSI in momentum zone for buy
    elif 35 < rsi < 55 and score < 0:
        score -= 0.4   # RSI in momentum zone for sell

    # Volume surge confirmation
    if f["cur_volume"] > f["avg_volume"] * 1.5:
        score *= 1.2   # amplify signal on volume spike

    # Volatility filter — ATR must be reasonable to scalp
    if atr < 0.0001:  # dead market
        return 0.0

    return max(-1.0, min(1.0, score))