from fastapi import FastAPI
import logging
from app.db import get_connection
from app.api.signals import router as signal_router

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
def market_data(data: dict):
    symbol = normalize_symbol(data.get("symbol", "UNKNOWN"))
    timeframe = data.get("timeframe", "M5").upper()

    insert_query = """
    INSERT INTO market_data
    (symbol, timeframe, timestamp, open, high, low, close, tick_volume, spread)
    VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, %s)
    """

    try:
        db_conn, db_cursor = get_connection()  # with auto-reconnect
        db_cursor.execute(insert_query, (
            symbol,
            timeframe,
            data.get("open"),
            data.get("high"),
            data.get("low"),
            data.get("close"),
            data.get("volume"),
            data.get("spread", 0)
        ))
        db_conn.commit()
        return {"status": "stored", "normalized_symbol": symbol}
    except Exception as e:
        logger.error(f"DB insert failed for {symbol} {timeframe}: {e}")
        try:
            db_conn.rollback()
        except Exception:
            pass
        return {
            "status": "accepted_but_not_stored",
            "normalized_symbol": symbol,
            "warning": "Data received but could not be persisted to database"
        }