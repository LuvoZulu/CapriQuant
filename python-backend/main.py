from fastapi import FastAPI, BackgroundTasks
import logging
import json
import pandas as pd
from app.db import conn, cursor
from app.api.signals import router as signal_router
from app.live_data import update_live_bar, get_recent_df
from app.features.builder import compute_structure
from app.engine.confluence import get_structure_signal, evaluate_setups

app = FastAPI(title="CapriQuant", version="2.0")

app.include_router(signal_router)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@app.get("/")
def home():
    return {"status": "quant system live"}


def normalize_symbol(symbol: str) -> str:
    """Normalize broker symbol names (e.g. XAUUSDm, XAUUSD#, XAUUSD.pro → XAUUSD)"""
    if not symbol:
        return symbol
    s = symbol.upper()
    # Common suffixes brokers add
    suffixes = ['M', '#', '.PRO', '.STD', '.ECN', '.RAW', 'PRO', 'STD']
    for suf in suffixes:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s


@app.post("/market-data")
def market_data(data: dict, background_tasks: BackgroundTasks):
    symbol = normalize_symbol(data.get("symbol", "UNKNOWN"))
    timeframe = data.get("timeframe", "M5").upper()

    # Log live data
    logger.info(f"[LIVE DATA] {symbol} {timeframe} | Close={data.get('close')} | Bid={data.get('bid')} | Ask={data.get('ask')} | Volume={data.get('volume')}")

    # 1. Update live buffer immediately (this is the fresh data we will use for decisions)
    bar = {
        "timestamp": data.get("timestamp"),
        "open": data.get("open"),
        "high": data.get("high"),
        "low": data.get("low"),
        "close": data.get("close"),
        "volume": data.get("volume"),
    }
    update_live_bar(symbol, bar)

    # 2. Try to compute real-time signal using recent live data (more aggressive for live path)
    signal_result = None
    try:
        recent_df = get_recent_df(symbol, min_bars=10)
        if recent_df is not None and len(recent_df) >= 6:
            # Use a very lenient min_candles for the live path so structure can start forming earlier
            ms = compute_structure(recent_df, symbol=symbol, timeframe=timeframe, min_candles=6)
            signal_result = get_structure_signal(ms, spread=data.get("spread", 0.0))
    except Exception as e:
        logger.error(f"Real-time structure processing failed for {symbol}: {e}")

    # 3. Return decision to MT5 immediately (this is what the EA will act on)
    response = {
        "status": "processed",
        "normalized_symbol": symbol,
        "timeframe": timeframe,
    }

    # Always inject the absolute latest price from the buffer
    try:
        from app.live_data import get_latest_price
        live = get_latest_price(symbol)
        if live:
            response["current_price"] = live["close"]
    except Exception:
        pass

    if signal_result:
        response["signal"] = signal_result
        # Pretty print the real-time signal we just computed from live data
        print(f"\n[REALTIME SIGNAL from POST] {symbol} {timeframe}", json.dumps(signal_result, indent=2, default=str))
    else:
        response["signal"] = {
            "signal": "HOLD",
            "confidence": 0.0,
            "rationale": "Insufficient live bars for structure analysis yet."
        }
        print(f"\n[REALTIME SIGNAL from POST] {symbol} {timeframe} → HOLD (not enough live bars yet)")

    # 4. Store to DB in background (after we already responded to MT5)
    def _persist_to_db():
        try:
            insert_query = """
            INSERT INTO market_data
            (symbol, timeframe, timestamp, open, high, low, close, tick_volume, spread)
            VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(insert_query, (
                symbol,
                timeframe,
                data.get("open"),
                data.get("high"),
                data.get("low"),
                data.get("close"),
                data.get("volume"),
                data.get("spread", 0)
            ))
            conn.commit()
        except Exception as e:
            logger.error(f"Background DB insert failed for {symbol}: {e}")
            try:
                conn.rollback()
            except:
                pass

    background_tasks.add_task(_persist_to_db)

    return response


# =============================================================================
# DEBUG ENDPOINTS - Live Buffer Inspection
# =============================================================================

@app.get("/debug/live-buffer")
def debug_live_buffer_all():
    """Returns how many bars are currently in the live buffer for each symbol."""
    from app.live_data import get_all_buffer_lengths
    return {
        "live_buffer_counts": get_all_buffer_lengths(),
        "note": "These are the number of recent M1 bars (completed + current) kept in memory for real-time structure analysis."
    }


@app.get("/debug/live-buffer/{symbol}")
def debug_live_buffer_symbol(symbol: str):
    """Returns detailed information about the live buffer for one symbol."""
    from app.live_data import get_buffer_info
    info = get_buffer_info(symbol)
    info["note"] = "This shows how much recent live data is available for real-time decision making."
    return info
