from fastapi import FastAPI
from app.db import conn, cursor
from app.api.signals import router as signal_router

app = FastAPI(title="CapriQuant", version="2.0")

app.include_router(signal_router)


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
    symbol = normalize_symbol(data["symbol"])
    timeframe = data["timeframe"].upper()

    insert_query = """
    INSERT INTO market_data
    (symbol, timeframe, timestamp, open, high, low, close, tick_volume, spread)
    VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, %s)
    """
    cursor.execute(insert_query, (
        symbol,
        timeframe,
        data["open"],
        data["high"],
        data["low"],
        data["close"],
        data["volume"],
        data.get("spread", 0)
    ))
    conn.commit()
    return {"status": "stored", "normalized_symbol": symbol}