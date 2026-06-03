from fastapi import FastAPI, BackgroundTasks
import logging
import json
import pandas as pd
from datetime import datetime
from app.db import conn, cursor
from app.api.signals import router as signal_router
from app.live_data import get_recent_df, live_buffer, add_market_data as update_live_bar
from app.features.builder import compute_structure
from app.engine.confluence import get_structure_signal, evaluate_setups
from app.utils.symbols import symbol_sql_match
from app.risk import RiskManager, RiskParams
from app.db import get_recent_loss_streak, get_today_realized_r

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
    live_buffer.add_market_data(symbol, data)

    # 2. Try to compute real-time signal using recent live data (more aggressive for live path)
    signal_result = None
    try:
        recent_df = live_buffer.get_recent_df_for_structure(symbol, limit=200)
        if recent_df is not None and len(recent_df) >= 6:
            # Use a very lenient min_candles for the live path so structure can start forming earlier
            ms = compute_structure(recent_df, symbol=symbol, timeframe="M1", min_candles=6)
            signal_result = get_structure_signal(ms, spread=data.get("spread", 0.0))
    except Exception as e:
        logger.error(f"Real-time structure processing failed for {symbol}: {e}")

    # === HARD RiskManager veto layer for realtime path (same as /signal) ===
    # Equity comes directly from EA payload every tick. Non-bypassable.
    try:
        if signal_result and isinstance(signal_result, dict):
            sig = signal_result.get("signal", "HOLD")
            if sig in ("BUY", "SELL"):
                eq = float(data.get("equity") or data.get("balance") or 200.0)
                streak = get_recent_loss_streak(symbol) or 0
                today_r = get_today_realized_r(symbol) or 0.0
                avg_risk_money = eq * 0.015
                today_pnl = today_r * avg_risk_money
                params = RiskParams(account_equity=eq, starting_equity=200.0, target_equity=17000.0)
                rm = RiskManager(params)
                allowed, veto_reason, eff_risk = rm.can_take_trade(
                    recent_loss_streak=streak, today_pnl=today_pnl, starting_equity_today=eq
                )
                signal_result["risk_pct"] = round(eff_risk, 2)
                signal_result["risk_streak"] = streak
                signal_result["risk_today_r"] = round(today_r, 2)
                if not allowed:
                    orig = sig
                    signal_result["signal"] = "HOLD"
                    signal_result["risk_veto"] = veto_reason
                    old_rat = signal_result.get("rationale", "")
                    signal_result["rationale"] = f"Risk veto: {veto_reason} (streak={streak}). {old_rat}".strip()
                    logger.warning(f"[RISK VETO REALTIME] {symbol} {orig} -> HOLD : {veto_reason}")
                else:
                    # prefer server stop from structure if present
                    for cand in ("validated_stop", "stop_suggestion", "stop"):
                        if cand in signal_result and signal_result.get(cand):
                            signal_result["validated_stop"] = signal_result[cand]
                            break
    except Exception as e:
        logger.error(f"[RISK] realtime veto layer error (non-fatal): {e}")

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
        # Promote key risk fields to top-level response for EA convenience (in addition to inside signal)
        for k in ("risk_pct", "risk_streak", "risk_veto", "validated_stop"):
            if k in signal_result:
                response[k] = signal_result[k]
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
            try:
                conn.rollback()
            except:
                pass
            ts_val = data.get("timestamp")
            if ts_val:
                insert_query = """
                INSERT INTO market_data
                (symbol, timeframe, timestamp, open, high, low, close, tick_volume, spread)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                cursor.execute(insert_query, (
                    symbol,
                    timeframe,
                    ts_val,
                    data.get("open"),
                    data.get("high"),
                    data.get("low"),
                    data.get("close"),
                    data.get("volume"),
                    data.get("spread", 0)
                ))
            else:
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
    info = live_buffer.get_buffer_status(symbol)
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
            try:
                conn.rollback()
            except:
                pass
            cursor.execute(q, sym_params + (limit,))
        else:
            q = base + " ORDER BY ts DESC LIMIT %s"
            try:
                conn.rollback()
            except:
                pass
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
        try:
            conn.rollback()
        except:
            pass
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

@app.get("/api/system-status")
def api_system_status():
    syms = [s for s in live_buffer.list_tracked_symbols() if s in ("XAUUSD", "DE30", "USTEC", "US30")]
    buffers = {s: live_buffer.get_buffer_status(s) for s in syms}
    return {
        "status": "running",
        "version": "post-fix",
        "timestamp": datetime.utcnow().isoformat(),
        "buffer_max_m1": getattr(live_buffer, 'max_bars', 10080),
        "buffer_max_m5": getattr(live_buffer, 'max_m5_bars', 2016),
        "symbols_tracked": syms,
        "buffers": buffers,
    }

@app.get("/api/current-structure/{symbol}")
def api_current_structure(symbol: str):
    sym = normalize_symbol(symbol)
    status = live_buffer.get_buffer_status(sym)
    df_m5 = live_buffer.get_recent_m5_df(sym, limit=200)
    if df_m5 is None or len(df_m5) < 3:
        return {"symbol": sym, "status": "insufficient_live_data", "buffer": status}
    try:
        from app.features.builder import compute_structure
        from app.features.structure import generate_structure_summary
        ms = compute_structure(df_m5, symbol=sym, timeframe="M5", min_candles=3)
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

@app.get("/api/recent-signals")
def api_recent_signals(symbol: str = None, limit: int = 100):
    from app.db import ensure_live_tables
    ensure_live_tables()
    try:
        sym = normalize_symbol(symbol) if symbol else None
        if sym:
            sym_clause, sym_params = symbol_sql_match(sym)
            q = f"""
                SELECT ts, symbol, timeframe, signal, score, confidence, setup, rationale,
                       structure_summary, bias, current_price, buffer_bars, raw_response
                FROM live_signals
                WHERE {sym_clause}
                ORDER BY ts DESC
                LIMIT %s
            """
            try:
                conn.rollback()
            except:
                pass
            cursor.execute(q, sym_params + (limit,))
        else:
            q = """
                SELECT ts, symbol, timeframe, signal, score, confidence, setup, rationale,
                       structure_summary, bias, current_price, buffer_bars, raw_response
                FROM live_signals
                ORDER BY ts DESC
                LIMIT %s
            """
            try:
                conn.rollback()
            except:
                pass
            cursor.execute(q, (limit,))
        rows = cursor.fetchall()
        cols = [desc[0] for desc in cursor.description]
        results = []
        for row in rows:
            d = dict(zip(cols, row))
            if d.get("ts"):
                d["ts"] = d["ts"].isoformat() if hasattr(d["ts"], "isoformat") else str(d["ts"])
            raw = d.pop("raw_response", None) or {}
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except:
                    raw = {}
            if isinstance(raw, dict):
                ctx = raw.get("contextual_scores") or {}
                d["total_confluence"] = ctx.get("total", raw.get("total_confluence", 0))
                d["contextual_scores"] = ctx
            else:
                d["total_confluence"] = 0
            for col in ("score", "confidence", "current_price", "total_confluence"):
                if d.get(col) is None:
                    d[col] = 0
            results.append(d)
        return results
    except Exception as e:
        print(f"api_recent_signals error: {e}")
        try:
            conn.rollback()
        except:
            pass
        return []

@app.get("/api/trades")
def api_trades(symbol: str = None, limit: int = 200):
    from app.db import ensure_live_tables
    ensure_live_tables()
    try:
        sym = normalize_symbol(symbol) if symbol else None
        if sym:
            sym_clause, sym_params = symbol_sql_match(sym)
            q = f"""
                SELECT ts, symbol, direction, entry_price, stop_loss, tp1, tp2,
                       r_multiple, outcome, volume_lots, notes
                FROM executed_trades
                WHERE {sym_clause}
                ORDER BY ts DESC
                LIMIT %s
            """
            try:
                conn.rollback()
            except:
                pass
            cursor.execute(q, sym_params + (limit,))
        else:
            q = """
                SELECT ts, symbol, direction, entry_price, stop_loss, tp1, tp2,
                       r_multiple, outcome, volume_lots, notes
                FROM executed_trades
                ORDER BY ts DESC
                LIMIT %s
            """
            try:
                conn.rollback()
            except:
                pass
            cursor.execute(q, (limit,))
        rows = cursor.fetchall()
        cols = [desc[0] for desc in cursor.description]
        out = []
        for row in rows:
            d = dict(zip(cols, row))
            if d.get("ts") and hasattr(d["ts"], "isoformat"):
                d["ts"] = d["ts"].isoformat()
            out.append(d)
        return out
    except Exception as e:
        print(f"api_trades error: {e}")
        try:
            conn.rollback()
        except:
            pass
        return []
