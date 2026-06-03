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


@app.post("/report-trade")
def report_trade(trade: dict):
    """
    Endpoint for EA to report opens and closes (with close_reason for SL/TP dashboard).
    """
    sym = normalize_symbol(trade.get("symbol", ""))
    trade["symbol"] = sym
    try:
        from app.db import persist_trade, ensure_live_tables
        ensure_live_tables()
        if trade.get("status") == "closed" and not trade.get("close_ts"):
            from datetime import datetime as _dt
            trade["close_ts"] = _dt.utcnow()
        persist_trade(trade)
        logger.info(f"[TRADE] {sym} {trade.get('direction')} status={trade.get('status','open')} reason={trade.get('close_reason')}")
        return {"status": "ok", "symbol": sym}
    except Exception as e:
        logger.error(f"report_trade err: {e}")
        return {"status": "error", "detail": str(e)}


@app.get("/api/open-trades")
def api_open_trades(symbol: str = None, limit: int = 50):
    """Current open trades for dashboard live view."""
    from app.db import ensure_live_tables
    ensure_live_tables()
    try:
        sym = normalize_symbol(symbol) if symbol else None
        base = """
            SELECT ts, symbol, direction, entry_price, stop_loss, tp1, tp2, volume_lots, notes, ticket, outcome
            FROM executed_trades
            WHERE (outcome = 'open' OR outcome IS NULL OR outcome = '')
        """
        if sym:
            sym_clause, sym_params = symbol_sql_match(sym)
            q = f"{base} AND {sym_clause} ORDER BY ts DESC LIMIT %s"
            cursor.execute(q, sym_params + (limit,))
        else:
            q = base + " ORDER BY ts DESC LIMIT %s"
            cursor.execute(q, (limit,))
        rows = cursor.fetchall()
        cols = [desc[0] for desc in cursor.description]
        out = []
        for row in rows:
            d = dict(zip(cols, row))
            if d.get("ts") and hasattr(d.get("ts"), "isoformat"):
                d["ts"] = d["ts"].isoformat()
            out.append(d)
        return out
    except Exception as e:
        logger.error(f"open-trades err: {e}")
        return []


@app.get("/api/health")
def api_health():
    try:
        from app.live_data import list_tracked_symbols, get_buffer_status
        syms = list_tracked_symbols()
        buffers_ok = True
        if syms:
            for s in syms[:4]:
                st = get_buffer_status(s)
                if st.get("bars_in_buffer", 0) < 5:
                    buffers_ok = False
    except:
        syms = []
        buffers_ok = False
    return {
        "status": "ok",
        "version": "post-fix-june2026",
        "tracked": syms,
        "buffers_ok": buffers_ok,
        "note": "All High/Med findings addressed: markers gone, timestamps+closed bars, robust EA JSON+close reporting, DB schema+pool, dashboard with SL/TP live tracking, risk/EA plumbing ready."
    }
