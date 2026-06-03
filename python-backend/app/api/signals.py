import pandas as pd
import json
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
    default_min = 15 if engine == "structure" else MIN_CANDLES_FOR_SIGNAL   # lowered for live bootstrapping
    min_required = min_candles_override if min_candles_override is not None else default_min

    # Safety floor - never allow less than 5 candles even in testing
    min_required = max(min_required, 5)

    if len(rows) < min_required:
        # We prefer to never 400 from /signal. The caller in get_trading_signal already
        # does a graceful early HOLD. This path is a safety net for other direct callers.
        raise HTTPException(
            status_code=400,
            detail=(
                f"Not enough data. Found only {len(rows)} candles for {normalized_symbol} {timeframe} "
                f"(need ≥ {min_required}). Use the data-feeder EA to populate the table first."
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
                "ready_for_default_structure": count >= 30,
                "ready_for_min_8 (what your EA uses)": count >= 8,
                "note": "Your EA currently requests with min_candles=8. Once candle_count >= 8, real structure signals can be generated (even if still weak)."
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
    tf_upper = timeframe.upper()

    # === Graceful insufficient-data handling (eliminates 400 spam) ===
    # Very common when data-feeder EAs and signal consumers start at the same time.
    # Return clean 200 + HOLD instead of hard 400.
    default_min = 15 if engine == "structure" else MIN_CANDLES_FOR_SIGNAL   # lowered from 30 for live data bootstrapping
    min_required = min_candles if min_candles is not None else default_min
    min_required = max(min_required, 5)

    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM market_data WHERE symbol = %s AND timeframe = %s",
        (normalized, tf_upper)
    )
    candles_available = cursor.fetchone()[0]
    cursor.close()

    if candles_available < min_required:
        friendly = {
            "signal": "HOLD",
            "score": 0.0,
            "confidence": 0.0,
            "engine": engine,
            "setup": None,
            "confluences": [],
            "rationale": (
                f"Insufficient market data for {normalized} {tf_upper}. "
                f"Only {candles_available} candles available (need ≥ {min_required}). "
                f"Keep your data-feeder EA(s) running — signals will start once we have more bars."
            ),
            "candles_available": candles_available,
            "min_required": min_required,
            "session": "UNKNOWN",
            "bias": "NEUTRAL",
        }
        response_body = {
            "symbol": normalized,
            "timeframe": tf_upper,
            "engine": engine,
            **friendly,
        }
        print(f"\n[SIGNAL RESPONSE] {normalized} {tf_upper}", json.dumps(response_body, indent=2, default=str))
        return response_body

    # Strongly prefer live aggregated data for real-time structure decisions
    # Use closed bars (no forming minute) for accurate structure (timestamp + accuracy fix)
    from app.live_data import get_recent_closed_df, get_recent_df, get_latest_price
    live_df = get_recent_closed_df(normalized, limit=200) or get_recent_df(normalized, min_bars=10)

    if live_df is not None and len(live_df) >= 6:
        df = live_df
        # Force the absolute latest price into the last bar for freshest decisions (analysis used closed)
        live_price = get_latest_price(normalized)
        if live_price and len(df) > 0:
            df.loc[df.index[-1], 'close'] = live_price['close']
            if 'high' in df.columns:
                df.loc[df.index[-1], 'high'] = max(df.loc[df.index[-1], 'high'], live_price['close'])
            if 'low' in df.columns:
                df.loc[df.index[-1], 'low'] = min(df.loc[df.index[-1], 'low'], live_price['close'])
    else:
        # Only fall back to DB if we truly have almost nothing in the live buffer
        df = fetch_candles(conn, symbol, tf_upper, engine=engine, min_candles_override=min_candles)

    if engine == "structure":
        ms = compute_structure(df, symbol=normalized, timeframe=timeframe, min_candles=min_candles or 10)
        result = get_structure_signal(ms, spread)
    else:
        features = compute_features(df)
        result = legacy_get_signal(features, spread)

    if engine == "structure":
        try:
            log_signal(result)
        except Exception:
            pass

    response_body = {
        "symbol": normalized,
        "timeframe": tf_upper,
        "engine": engine,
        **result,
    }

    # === Force live price into the response for real-time feel ===
    try:
        from app.live_data import get_latest_price
        live = get_latest_price(normalized)
        if live:
            response_body["current_price"] = live["close"]
            # Also update inside market_structure if present
            if "market_structure" in response_body:
                response_body["market_structure"]["current_price"] = live["close"]
    except Exception:
        pass

    print(f"\n[SIGNAL RESPONSE] {normalized} {tf_upper}", json.dumps(response_body, indent=2, default=str))
    return response_body
