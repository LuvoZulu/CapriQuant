from fastapi import FastAPI, BackgroundTasks
import logging
import json
from datetime import datetime
from app.db import conn, cursor
from app.api.signals import router as signal_router
from app.live_data import live_buffer
from app.features.builder import compute_structure
from app.engine.confluence import get_structure_signal
from app.features.structure import generate_structure_summary

app = FastAPI(title="CapriQuant", version="2.1-realtime")

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
    """
    Real-time ingestion + immediate structure signal for TICK data.
    Uses the 10080-bar rolling live buffer.
    """
    symbol = normalize_symbol(data.get("symbol", "UNKNOWN"))
    timeframe = str(data.get("timeframe", "TICK")).upper()
    spread = float(data.get("spread", 0))

    # Log
    logger.info(f"[LIVE DATA] {symbol} {timeframe} | Close={data.get('close')} | Bid={data.get('bid')} | Ask={data.get('ask')} | Volume={data.get('volume')}")

    # Feed the live buffer (new class API)
    completed = live_buffer.add_market_data(symbol, data)

    # Compute realtime signal on live buffer when it's TICK data
    signal_result = None
    try:
        if timeframe == "TICK":
            df = live_buffer.get_recent_df(symbol, limit=10080)
            current_price = float(data.get("close") or data.get("bid") or 0)
            if df is not None and len(df) >= 8:
                ms = compute_structure(df, symbol=symbol, timeframe="TICK", min_candles=8)
                ms.current_price = current_price
                sig = get_structure_signal(ms, spread=spread)
                # ensure nice summary
                if not sig.get("structure_summary"):
                    sig["structure_summary"] = generate_structure_summary(ms)
                sig["current_price"] = current_price
                sig["realtime"] = True
                sig["buffer_status"] = live_buffer.get_buffer_status(symbol)
                signal_result = sig
                print(f"\n[REALTIME SIGNAL from POST] {symbol} TICK", json.dumps(sig, indent=2, default=str)[:600])
            else:
                buf_status = live_buffer.get_buffer_status(symbol)
                signal_result = {
                    "signal": "HOLD",
                    "score": 0.0,
                    "confidence": 0.0,
                    "engine": "structure_v2_strict",
                    "setup": None,
                    "confluences": [],
                    "rationale": f"Not enough live M1 bars yet ({buf_status.get('bars_in_buffer', 0)}/10080).",
                    "structure_summary": f"Building live buffer... {buf_status.get('bars_in_buffer', 0)} / 10080 M1 bars",
                    "session": "UNKNOWN",
                    "bias": "NEUTRAL",
                    "current_price": current_price,
                    "realtime": True,
                    "buffer_status": buf_status,
                }
                print(f"[REALTIME SIGNAL from POST] {symbol} TICK → HOLD (building buffer)")
    except Exception as e:
        logger.error(f"Real-time structure processing failed for {symbol}: {e}")

    # Build response
    response = {
        "status": "processed",
        "normalized_symbol": symbol,
        "timeframe": timeframe,
    }

    # latest price
    try:
        latest = live_buffer.get_recent_bars(symbol)
        if latest:
            response["current_price"] = latest[-1].close
    except Exception:
        pass

    if signal_result:
        response["signal"] = signal_result
    else:
        response["signal"] = {
            "signal": "HOLD",
            "confidence": 0.0,
            "rationale": "No realtime signal computed this tick."
        }

    # Background DB persist of the raw payload (original behavior)
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
                data.get("open", 0),
                data.get("high", 0),
                data.get("low", 0),
                data.get("close", 0),
                data.get("volume", 0),
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
# DEBUG + DASHBOARD API ENDPOINTS
# =============================================================================

@app.get("/debug/live-buffer")
def debug_live_buffer(symbol: str = None):
    if symbol:
        sym = normalize_symbol(symbol)
        return {
            "symbol": sym,
            "status": live_buffer.get_buffer_status(sym),
            "recent_bars_count": len(live_buffer.get_recent_bars(sym)),
        }
    all_status = {sym: live_buffer.get_buffer_status(sym) for sym in list(live_buffer.buffers.keys())}
    return {"all_symbols": all_status, "global_max_m1": live_buffer.max_bars}


@app.get("/api/system-status")
def api_system_status():
    return {
        "status": "running",
        "version": "2.1-realtime",
        "timestamp": datetime.utcnow().isoformat(),
        "buffer_max_m1": live_buffer.max_bars,
        "symbols_tracked": list(live_buffer.buffers.keys()),
    }


@app.get("/api/recent-signals")
def api_recent_signals(symbol: str = None, limit: int = 100):
    """For the UI signal history and build-up charts."""
    from app.db import ensure_live_tables
    ensure_live_tables()
    try:
        q = """
            SELECT ts, symbol, timeframe, signal, score, confidence, setup, rationale,
                   structure_summary, bias, current_price, buffer_bars
            FROM live_signals
            WHERE (%s IS NULL OR symbol = %s)
            ORDER BY ts DESC
            LIMIT %s
        """
        cursor.execute(q, (symbol, symbol, limit))
        rows = cursor.fetchall()
        cols = [desc[0] for desc in cursor.description]
        return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        return {"error": str(e), "data": []}


@app.get("/api/trades")
def api_trades(symbol: str = None, limit: int = 200):
    """For the Trades section of the UI."""
    from app.db import ensure_live_tables
    ensure_live_tables()
    try:
        q = """
            SELECT ts, symbol, direction, entry_price, stop_loss, tp1, tp2,
                   r_multiple, outcome, volume_lots, notes
            FROM executed_trades
            WHERE (%s IS NULL OR symbol = %s)
            ORDER BY ts DESC
            LIMIT %s
        """
        cursor.execute(q, (symbol, symbol, limit))
        rows = cursor.fetchall()
        cols = [desc[0] for desc in cursor.description]
        return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        return {"error": str(e), "data": []}


@app.get("/api/current-structure/{symbol}")
def api_current_structure(symbol: str):
    sym = normalize_symbol(symbol)
    df = live_buffer.get_recent_df(sym, limit=200)
    status = live_buffer.get_buffer_status(sym)
    if df is None or len(df) < 5:
        return {"symbol": sym, "status": "insufficient_live_data", "buffer": status}
    try:
        ms = compute_structure(df, symbol=sym, timeframe="M1", min_candles=5)
        summary = generate_structure_summary(ms)
        return {
            "symbol": sym,
            "current_price": ms.current_price,
            "bias": ms.bias,
            "structure_summary": summary,
            "active_bullish_obs": len([o for o in ms.order_blocks if getattr(o, "ob_type", "") == "BULLISH" and not getattr(o, "is_mitigated", True)]),
            "active_bearish_obs": len([o for o in ms.order_blocks if getattr(o, "ob_type", "") == "BEARISH" and not getattr(o, "is_mitigated", True)]),
            "swing_count": len(getattr(ms, "swings", [])),
            "buffer": status,
        }
    except Exception as e:
        return {"symbol": sym, "error": str(e), "buffer": status}


@app.post("/report-trade")
def report_trade(trade: dict):
    """
    Endpoint for the AutoTrader EA to report filled trades.
    Powers the Trades section of the UI.
    """
    sym = normalize_symbol(trade.get("symbol", ""))
    trade["symbol"] = sym
    try:
        from app.db import persist_trade
        persist_trade(trade)
        return {"status": "trade_recorded", "symbol": sym}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
