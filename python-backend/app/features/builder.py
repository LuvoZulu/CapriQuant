import pandas as pd
import numpy as np

from app.features.structure import (
    compute_market_structure,
    MarketStructure,
    market_structure_to_legacy_features,
)


def compute_features(df: pd.DataFrame) -> dict:
    """
    Takes a DataFrame of OHLCV candles (200 rows),
    returns a dict of computed features for all strategies.
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    volume = df["volume"]

    features = {}

    # ── EMAs ──────────────────────────────────────────────
    features["ema_9"]  = close.ewm(span=9,  adjust=False).mean().iloc[-1]
    features["ema_21"] = close.ewm(span=21, adjust=False).mean().iloc[-1]
    features["ema_50"] = close.ewm(span=50, adjust=False).mean().iloc[-1]
    features["ema_200"]= close.ewm(span=200,adjust=False).mean().iloc[-1]

    # ── RSI ───────────────────────────────────────────────
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    features["rsi"] = float((100 - 100 / (1 + rs)).iloc[-1])

    # ── MACD ──────────────────────────────────────────────
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line   = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    features["macd"]        = float(macd_line.iloc[-1])
    features["macd_signal"] = float(signal_line.iloc[-1])
    features["macd_hist"]   = float((macd_line - signal_line).iloc[-1])

    # ── ATR ───────────────────────────────────────────────
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    features["atr"] = float(tr.rolling(14).mean().iloc[-1])

    # ── Swing Highs / Lows (last 20 candles) ─────────────
    window = 20
    features["swing_high"] = float(high.iloc[-window:].max())
    features["swing_low"]  = float(low.iloc[-window:].min())

    # ── Fibonacci Levels ──────────────────────────────────
    swing_range = features["swing_high"] - features["swing_low"]
    features["fib_236"] = features["swing_high"] - swing_range * 0.236
    features["fib_382"] = features["swing_high"] - swing_range * 0.382
    features["fib_500"] = features["swing_high"] - swing_range * 0.500
    features["fib_618"] = features["swing_high"] - swing_range * 0.618
    features["fib_786"] = features["swing_high"] - swing_range * 0.786

    # ── Breakout Levels (last 50 candles) ─────────────────
    features["range_high"] = float(high.iloc[-50:].max())
    features["range_low"]  = float(low.iloc[-50:].min())

    # ── Current price snapshot ────────────────────────────
    features["current_close"] = float(close.iloc[-1])
    features["prev_close"]    = float(close.iloc[-2])
    features["current_high"]  = float(high.iloc[-1])
    features["current_low"]   = float(low.iloc[-1])

    # ── Volume ────────────────────────────────────────────
    features["avg_volume"] = float(volume.rolling(20).mean().iloc[-1])
    features["cur_volume"] = float(volume.iloc[-1])

    # ── Session Phase (AMD) ───────────────────────────────
    last_ts = df["timestamp"].iloc[-1]
    hour = pd.to_datetime(last_ts).hour
    if 2 <= hour < 8:
        features["session"] = "accumulation"   # Asian session
    elif 8 <= hour < 12:
        features["session"] = "manipulation"   # London open
    elif 12 <= hour < 17:
        features["session"] = "distribution"   # NY session
    else:
        features["session"] = "off"

    # ── Candle Pattern ────────────────────────────────────
    body      = abs(close.iloc[-1] - df["open"].iloc[-1])
    candle_range = high.iloc[-1] - low.iloc[-1]
    features["body_ratio"] = body / candle_range if candle_range > 0 else 0

    # Engulfing
    prev_body  = abs(close.iloc[-2] - df["open"].iloc[-2])
    curr_body  = abs(close.iloc[-1] - df["open"].iloc[-1])
    bull_engulf = (close.iloc[-1] > df["open"].iloc[-1] and
                   close.iloc[-2] < df["open"].iloc[-2] and
                   curr_body > prev_body)
    bear_engulf = (close.iloc[-1] < df["open"].iloc[-1] and
                   close.iloc[-2] > df["open"].iloc[-2] and
                   curr_body > prev_body)
    features["bull_engulfing"] = bull_engulf
    features["bear_engulfing"] = bear_engulf

    # Pin bar
    upper_wick = high.iloc[-1] - max(close.iloc[-1], df["open"].iloc[-1])
    lower_wick = min(close.iloc[-1], df["open"].iloc[-1]) - low.iloc[-1]
    features["pin_bar_bull"] = lower_wick > 2 * body and lower_wick > upper_wick
    features["pin_bar_bear"] = upper_wick > 2 * body and upper_wick > lower_wick

    return features


# =============================================================================
# NEW STRUCTURE-FIRST ENGINE (Recommended path forward)
# =============================================================================

def compute_structure(df: pd.DataFrame, symbol: str = "XAUUSD", timeframe: str = "M5", min_candles: int = 30) -> MarketStructure:
    """
    Modern replacement for compute_features.

    Returns rich MarketStructure with proper swings, order blocks, BOS/CHOCH,
    liquidity, FVGs, and real session AMD analysis.

    Use this for all new strategy and signal logic.
    """
    return compute_market_structure(df, symbol=symbol, timeframe=timeframe, min_candles=min_candles)


def get_enriched_features(df: pd.DataFrame, symbol: str = "XAUUSD", timeframe: str = "M5", min_candles: int = 30) -> dict:
    """
    Bridge for gradual migration.
    Returns the old flat dict shape PLUS new structure fields.
    """
    ms = compute_market_structure(df, symbol=symbol, timeframe=timeframe, min_candles=min_candles)
    legacy = market_structure_to_legacy_features(ms, df)

    # Merge and mark that this came from the new engine
    legacy["structure_engine"] = True
    legacy["market_structure"] = ms.to_dict()
    legacy["bias"] = ms.bias
    legacy["active_bull_obs"] = legacy.get("active_bull_obs", 0)
    legacy["active_bear_obs"] = legacy.get("active_bear_obs", 0)
    legacy["manipulation_detected"] = ms.session.manipulation_detected
    return legacy