from fastapi import FastAPI
import json
from datetime import datetime

from app.db import conn, cursor
from app.api.signals import router as signal_router
from app.live_data import live_buffer, CompletedBar
from app.features.builder import compute_structure
from app.engine.confluence import get_structure_signal
from app.features.structure import generate_structure_summary

app = FastAPI(title="CapriQuant", version="2.1-realtime")

app.include_router(signal_router)


@app.get("/")
def home():
    return {"status": "quant system live", "realtime": True, "buffer_max_m1": 10080}


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


def _persist_bar(bar: CompletedBar, spread: float = 0.0):
    """Persist a completed M1 bar (from live aggregation) to the market_data table."""
    try:
        insert_query = """
        INSERT INTO market_data
        (symbol, timeframe, timestamp, open, high, low, close, tick_volume, spread)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """
        # Note: if your Postgres doesn't have unique constraint on (symbol, timeframe, timestamp) the ON CONFLICT is harmless or remove it.
        cursor.execute(insert_query, (
            bar.symbol,
            bar.timeframe,
            bar.timestamp,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            spread
        ))
        conn.commit()
    except Exception:
        # Best effort persistence; don't break the realtime path
        conn.rollback()


@app.post("/market-data")
def market_data(data: dict):
    """
    Enhanced real-time ingestion point.

    - Always stores the raw payload (for historical audit).
    - For TICK payloads: aggregates into completed M1 bars using the rolling live buffer (now 10080 bars).
    - When a new M1 bar is completed, it is persisted.
    - Immediately runs the full structure engine on the live buffer + current price.
    - Returns the rich signal (with structure_summary, market_structure, confluences etc.) in the response
      so the EA gets low-latency decisions without waiting for the next M5 poll.
    """
    raw_symbol = data.get("symbol", "UNKNOWN")
    symbol = normalize_symbol(raw_symbol)
    timeframe = str(data.get("timeframe", "TICK")).upper()
    spread = float(data.get("spread", data.get("spread_points", 0)))

    # 1. Always attempt to store the raw incoming payload (TICK or bar) for full audit trail
    try:
        insert_query = """
        INSERT INTO market_data
        (symbol, timeframe, timestamp, open, high, low, close, tick_volume, spread)
        VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(insert_query, (
            symbol,
            timeframe,
            data.get("open", 0),
            data.get("high", 0),
            data.get("low", 0),
            data.get("close", 0),
            data.get("volume", 0),
            spread
        ))
        conn.commit()
    except Exception:
        conn.rollback()

    # 2. Feed the live buffer (critical for real-time structure on TICKs)
    completed_bar: CompletedBar | None = live_buffer.add_market_data(symbol, data)

    if completed_bar:
        _persist_bar(completed_bar, spread)

    # 3. Real-time signal path for TICK data (matches the behavior the EA expects from the logs)
    if timeframe == "TICK":
        try:
            bars_df = live_buffer.get_recent_df(symbol, limit=10080)
            current_price = float(data.get("close") or data.get("bid") or data.get("last") or 0.0)

            if len(bars_df) >= 8:
                ms = compute_structure(
                    bars_df,
                    symbol=symbol,
                    timeframe="TICK",   # live view
                    min_candles=8
                )
                # Make sure the very latest tick price is reflected
                ms.current_price = current_price

                signal = get_structure_signal(ms, spread=spread)

                # Guarantee structure_summary is present (used heavily by UI and logs)
                if "structure_summary" not in signal or not signal.get("structure_summary"):
                    signal["structure_summary"] = generate_structure_summary(ms)

                signal["current_price"] = current_price
                signal["realtime"] = True
                buf_status = live_buffer.get_buffer_status(symbol)
                signal["buffer_status"] = buf_status

                # Persist for UI historical + "signal building progress" charts
                try:
                    from app.db import persist_signal
                    persist_signal(signal, symbol, "TICK", buffer_bars=buf_status.get("bars_in_buffer", 0))
                except Exception:
                    pass

                print(f"\n[REALTIME SIGNAL from POST] {symbol} TICK {json.dumps(signal, indent=2, default=str)[:500]}...")
                return signal
            else:
                # Graceful early HOLD while the live buffer warms up (important for 24/7 restarts)
                summary = f"Building live buffer... {len(bars_df)} / 10080 M1 bars"
                print(f"[LIVE DATA] {symbol} TICK | Close={current_price} | buffer={len(bars_df)}")
                return {
                    "signal": "HOLD",
                    "score": 0.0,
                    "confidence": 0.0,
                    "engine": "structure_v2_strict",
                    "setup": None,
                    "confluences": [],
                    "rationale": "Not enough live M1 bars yet for reliable structure (need >= 8).",
                    "structure_summary": summary,
                    "session": "UNKNOWN",
                    "bias": "NEUTRAL",
                    "current_price": current_price,
                    "realtime": True,
                    "buffer_status": live_buffer.get_buffer_status(symbol),
                    "market_structure": {"symbol": symbol, "timeframe": "TICK", "current_price": current_price},
                }
        except Exception as e:
            print(f"ERROR:main:Real-time structure processing failed for {symbol}: {e}")
            # Fall through to basic stored response

    # 4. Default / non-TICK response (M1/M5 bars from EA or other feeders)
    buf_status = live_buffer.get_buffer_status(symbol)
    print(f"INFO:main:[LIVE DATA] {symbol} {timeframe} | Close={data.get('close')} | buffer={buf_status['bars_in_buffer']}")
    return {
        "status": "stored",
        "normalized_symbol": symbol,
        "timeframe": timeframe,
        "realtime_buffer": buf_status,
    }


@app.post("/report-trade")
def report_trade(trade: dict):
    """
    Endpoint for the AutoTrader EA (or any executor) to report filled / managed trades.
    Powers the 'Trades' section of the UI and historical performance tracking.
    """
    sym = normalize_symbol(trade.get("symbol", ""))
    trade["symbol"] = sym

    try:
        persist_trade(trade)
        return {"status": "trade_recorded", "symbol": sym}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# Debug endpoint for the live buffer (very useful for the UI and debugging)
@app.get("/debug/live-buffer")
def debug_live_buffer(symbol: str = None):
    if symbol:
        sym = normalize_symbol(symbol)
        return {
            "symbol": sym,
            "status": live_buffer.get_buffer_status(sym),
            "recent_bars_count": len(live_buffer.get_recent_bars(sym)),
        }
    # Aggregate view across symbols
    all_status = {}
    for sym in list(live_buffer.buffers.keys()):
        all_status[sym] = live_buffer.get_buffer_status(sym)
    return {"all_symbols": all_status, "global_max_m1": live_buffer.max_bars}
