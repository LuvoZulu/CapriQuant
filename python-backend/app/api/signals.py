import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from app.features.builder import compute_features, compute_structure, get_enriched_features
from app.consensus import get_signal as legacy_get_signal
from app.engine.confluence import get_structure_signal
from app.utils.signal_logger import log_signal

router = APIRouter()

CANDLE_LIMIT = 200
MIN_CANDLES_FOR_SIGNAL = 50  # You can lower this for testing if needed


def normalize_symbol(symbol: str) -> str:
    """Normalize broker symbol names (e.g. XAUUSDm, XAUUSD#, XAUUSD.pro → XAUUSD)"""
    if not symbol:
        return symbol
    s = symbol.upper()
    suffixes = ['M', '#', '.PRO', '.STD', '.ECN', '.RAW', 'PRO', 'STD']
    for suf in suffixes:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s


def fetch_candles(conn, symbol: str, timeframe: str, engine: str = "legacy", min_candles_override: int = None) -> pd.DataFrame:
    normalized_symbol = normalize_symbol(symbol)

    query = """
        SELECT timestamp, open, high, low, close, tick_volume as volume
        FROM market_data
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp DESC
        LIMIT %s
    """
    cursor = conn.cursor()
    cursor.execute(query, (normalized_symbol, timeframe, CANDLE_LIMIT))
    rows = cursor.fetchall()
    cursor.close()

    # Default minimums
    default_min = 30 if engine == "structure" else MIN_CANDLES_FOR_SIGNAL
    min_required = min_candles_override if min_candles_override is not None else default_min

    # Safety floor - never allow less than 5 candles even in testing
    min_required = max(min_required, 5)

    if len(rows) < min_required:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Not enough data for signal. "
                f"Found only {len(rows)} candles for normalized symbol '{normalized_symbol}' "
                f"(you requested '{symbol}'). "
                f"Need at least {min_required} candles. "
                f"You can bypass this during Strategy Tester with ?min_candles=10"
            )
        )

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.iloc[::-1].reset_index(drop=True)  # oldest first
    return df


@router.get("/debug/data-count")
def get_data_count(symbol: str = None, timeframe: str = None):
    """Debug endpoint to see how much data exists for a symbol/timeframe"""
    from app.db import conn
    cursor = conn.cursor()

    if symbol:
        normalized = normalize_symbol(symbol)
        if timeframe:
            cursor.execute(
                "SELECT COUNT(*) FROM market_data WHERE symbol = %s AND timeframe = %s",
                (normalized, timeframe.upper())
            )
            count = cursor.fetchone()[0]
            cursor.close()
            return {
                "normalized_symbol": normalized,
                "timeframe": timeframe.upper(),
                "candle_count": count,
                "ready_for_signal": count >= 50
            }
        else:
            cursor.execute(
                "SELECT timeframe, COUNT(*) FROM market_data WHERE symbol = %s GROUP BY timeframe",
                (normalized,)
            )
            rows = cursor.fetchall()
            cursor.close()
            return {"normalized_symbol": normalized, "by_timeframe": dict(rows)}
    else:
        cursor.execute("SELECT symbol, timeframe, COUNT(*) FROM market_data GROUP BY symbol, timeframe ORDER BY symbol, timeframe")
        rows = cursor.fetchall()
        cursor.close()
        return {"all_data": [{"symbol": r[0], "timeframe": r[1], "count": r[2]} for r in rows]}


@router.get("/signal/{symbol}/{timeframe}")
def get_trading_signal(
    symbol: str,
    timeframe: str,
    spread: float = 0.0,
    engine: str = Query("legacy", description="legacy | structure"),
    min_candles: int = Query(
        None, 
        description="Temporarily lower the minimum candles needed (e.g. ?min_candles=10). Only for Strategy Tester / testing."
    ),
):
    from app.db import conn

    normalized = normalize_symbol(symbol)
    df = fetch_candles(conn, symbol, timeframe.upper(), engine=engine, min_candles_override=min_candles)

    if engine == "structure":
        # Pass through the min_candles override if provided (useful for Strategy Tester)
        ms = compute_structure(df, symbol=normalized, timeframe=timeframe, min_candles=min_candles or 30)
        result = get_structure_signal(ms, spread)
    else:
        features = compute_features(df)
        result = legacy_get_signal(features, spread)

    if engine == "structure":
        try:
            log_signal(result)
        except Exception:
            pass

    return {
        "symbol": normalized,
        "timeframe": timeframe.upper(),
        "engine": engine,
        **result,
    }