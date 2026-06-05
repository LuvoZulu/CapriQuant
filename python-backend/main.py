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
from app.engine.multi_timeframe import get_mtf_structure_signal
from app.utils.symbols import symbol_sql_match, normalize_symbol as _normalize_symbol
from app.risk import RiskManager, RiskParams
from app.db import get_recent_loss_streak, get_today_realized_r
from app.engine.management import compute_managements_for_all_opens, compute_management_for_open
from app.config import get_settings
from app.system_mode import (
    get_system_mode,
    set_system_mode,
    _apply_system_mode_to_signal,
    _flatten_signal_for_ea,
    _compute_current_alerts,
    record_quality_bad,
    get_quality_issues,
)

import uuid
# Note: system mode logic moved to app/system_mode.py (no circular imports, single source)

app = FastAPI(title="CapriQuant", version="2.0")

app.include_router(signal_router)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@app.get("/")
def home():
    return {"status": "quant system live"}


# Delegate to canonical implementation (single source of truth in app/utils/symbols.py)
def normalize_symbol(symbol: str) -> str:
    return _normalize_symbol(symbol)


@app.post("/market-data")
def market_data(data: dict, background_tasks: BackgroundTasks):
    symbol = normalize_symbol(data.get("symbol", "UNKNOWN"))
    timeframe = data.get("timeframe", "M5").upper()
    if timeframe == "TICK":
        timeframe = "M1"
    is_backfill = data.get("backfill") in (True, "true", 1, "1", "True")
    req_id = str(uuid.uuid4())[:8]

    # === Data Quality Gate (phase2) - reject poison early ===
    try:
        qclose = float(data.get("close") or 0)
        qspread = float(data.get("spread") or data.get("ask", 0) - data.get("bid", 0) or 0)
        qts = data.get("timestamp")
        qvol = float(data.get("volume") or 0)
        bad_reasons = []
        if qclose <= 0:
            bad_reasons.append("price<=0")
        # Reasonable bounds (extendable via config later)
        if symbol in ("XAUUSD", "XAU") and not (500 < qclose < 5000):
            bad_reasons.append("xau_price_out_of_range")
        if symbol in ("US30", "USTEC", "DE30") and not (5000 < qclose < 100000):
            bad_reasons.append("index_price_out_of_range")
        if qspread < 0 or qspread > 1000:  # generous for indices
            bad_reasons.append(f"bad_spread:{qspread}")
        if qts:
            # basic future/past check (allow some clock skew)
            try:
                from datetime import datetime as _dt
                if isinstance(qts, (int, float)):
                    ts_dt = _dt.utcfromtimestamp(qts / 1000 if qts > 1e12 else qts)
                else:
                    ts_dt = _dt.fromisoformat(str(qts).replace("Z", "+00:00"))
                now = _dt.utcnow()
                if (ts_dt - now).total_seconds() > 300:
                    bad_reasons.append("future_timestamp")
                if (now - ts_dt).total_seconds() > 86400 * 1:
                    bad_reasons.append("ancient_timestamp")
            except:
                pass
        if qvol < 0:
            bad_reasons.append("negative_volume")
        if bad_reasons:
            logger.warning(f"[DATA_QUALITY] {symbol} rejected: {bad_reasons} raw={data}")
            # Still allow buffer for resilience in early dev, but mark & don't compute signal on bad
            data["_quality_bad"] = bad_reasons
            # record for status (delegated to shared module)
            record_quality_bad(symbol, bad_reasons)
    except Exception as _qe:
        logger.debug(f"quality gate error (non fatal): {_qe}")

    # Log live data (with correlation id)
    logger.info(f"[LIVE DATA {req_id}] {symbol} {timeframe} | Close={data.get('close')} | Bid={data.get('bid')} | Ask={data.get('ask')} | Volume={data.get('volume')}")

    # Historical catch-up: never older than 24h — buffer + DB only, no signals/trades
    if is_backfill:
        from app.live_data import is_within_catchup_window, to_naive_utc
        ts_raw = data.get("timestamp")
        if ts_raw is not None and not is_within_catchup_window(to_naive_utc(ts_raw)):
            logger.info(f"[BACKFILL] skipped stale bar for {symbol}: {ts_raw}")
            return {
                "status": "backfill_skipped_stale",
                "normalized_symbol": symbol,
                "timeframe": timeframe,
                "max_lookback_hours": get_settings().catchup_max_hours,
            }
        live_buffer.add_market_data(symbol, data)

        def _persist_backfill():
            from app.db import db_cursor
            try:
                with db_cursor() as (c, cur):
                    ts_val = data.get("timestamp")
                    if ts_val:
                        cur.execute(
                            """
                            INSERT INTO market_data
                            (symbol, timeframe, timestamp, open, high, low, close, tick_volume, spread)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                symbol, timeframe, ts_val,
                                data.get("open"), data.get("high"), data.get("low"),
                                data.get("close"), data.get("volume"), data.get("spread", 0),
                            ),
                        )
                    c.commit()
            except Exception as e:
                logger.error(f"Background backfill DB insert failed for {symbol}: {e}")

        background_tasks.add_task(_persist_backfill)
        return {
            "status": "backfill_stored",
            "normalized_symbol": symbol,
            "timeframe": timeframe,
            "max_lookback_hours": get_settings().catchup_max_hours,
        }

    # 1. Update live buffer for live ticks only (backfill handled above)
    live_buffer.add_market_data(symbol, data)

    # 2. Try to compute real-time signal using recent live data (more aggressive for live path)
    # Prefer full MTF production path when buffers allow (best for system)
    signal_result = None
    if data.get("_quality_bad"):
        logger.info(f"[DATA_QUALITY] skipping realtime structure for {symbol} due to {data['_quality_bad']}")
    else:
        try:
            # Always prefer full MTF production path by default (best accuracy for the system)
            # Fallback to single-TF only on insufficient higher-TF closed data
            signal_result = get_mtf_structure_signal(symbol, spread=data.get("spread", 0.0), min_candles_m1=6, equity=data.get("equity", 0))
            if signal_result is None or (signal_result.get("engine") == "structure_mtf_precision" and signal_result.get("signal") == "HOLD" and "Building" in str(signal_result.get("rationale", ""))):
                recent_df = live_buffer.get_recent_df_for_structure(symbol, limit=200)
                if recent_df is not None and len(recent_df) >= 6:
                    ms = compute_structure(recent_df, symbol=symbol, timeframe="M1", min_candles=6)
                    signal_result = get_structure_signal(ms, spread=data.get("spread", 0.0))
                    signal_result["engine"] = "structure_fallback_single"
        except Exception as e:
            logger.error(f"Real-time structure processing failed for {symbol}: {e}")

    # === HARD RiskManager veto layer for realtime path (same as /signal) ===
    # Equity comes directly from EA payload every tick. Non-bypassable.
    try:
        if signal_result and isinstance(signal_result, dict):
            sig = signal_result.get("signal", "HOLD")
            if sig in ("BUY", "SELL"):
                eq = float(data.get("equity") or data.get("balance") or 200.0)
                # Account-level (global) streak + daily PnL for hard circuits — not per-symbol
                streak = get_recent_loss_streak(None) or 0
                today_r = get_today_realized_r(None) or 0.0
                s = get_settings()
                avg_risk_money = eq * (s.risk_daily_pnl_proxy_pct / 100.0)
                today_pnl = today_r * avg_risk_money
                params = RiskParams(
                    account_equity=eq,
                    starting_equity=s.risk_starting_equity,
                    target_equity=s.risk_target_equity,
                    max_daily_loss_pct=s.risk_max_daily_loss_pct,
                )
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
                    from app.api.signals import _resolve_validated_stop
                    vstop = _resolve_validated_stop(signal_result)
                    if vstop:
                        signal_result["validated_stop"] = vstop
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
        # Apply kill switch / system mode override (non-bypassable)
        signal_result = _apply_system_mode_to_signal(signal_result, symbol)
        response["signal"] = signal_result
        _flatten_signal_for_ea(response, signal_result)
        # Pretty print the real-time signal we just computed from live data
        print(f"\n[REALTIME SIGNAL from POST] {symbol} {timeframe}", json.dumps(signal_result, indent=2, default=str))
        # Promote key risk fields to top-level response for EA convenience (in addition to inside signal)
        for k in ("risk_pct", "risk_streak", "risk_veto", "validated_stop", "system_mode", "action"):
            if k in signal_result:
                response[k] = signal_result[k]
        try:
            from app.utils.signal_logger import log_signal
            log_signal(signal_result, symbol=symbol, timeframe=timeframe)
        except Exception:
            pass

        # Post-entry management suggestions for any current opens on this symbol (best for the system)
        try:
            from app.db import db_cursor
            from app.utils.symbols import symbol_sql_match
            sym_clause, sym_params = symbol_sql_match(symbol)
            with db_cursor() as (c, cur):
                cur.execute(f"""
                    SELECT ts, symbol, direction, entry_price, stop_loss, tp1, tp2, volume_lots, notes, ticket, outcome
                    FROM executed_trades
                    WHERE {sym_clause} AND (outcome = 'open' OR outcome IS NULL OR outcome = '')
                    ORDER BY ts DESC LIMIT 5
                """, sym_params)
                opens = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
            if opens:
                from app.features.builder import compute_structure
                from app.live_data import filter_df_to_catchup_window
                df_m5 = live_buffer.get_recent_m5_df(symbol, limit=200)
                df_m5 = filter_df_to_catchup_window(df_m5)
                if df_m5 is not None and len(df_m5) >= 5:
                    ms = compute_structure(df_m5, symbol=symbol, timeframe="M5", min_candles=5)
                    mgmts = compute_managements_for_all_opens(opens, {symbol: ms}, get_system_mode())
                    if mgmts:
                        response["management"] = mgmts[0]  # primary for this symbol
                        # also promote to signal for EA parsing
                        signal_result["management"] = mgmts[0]
        except Exception as _me:
            pass  # non fatal
    else:
        base_hold = {
            "signal": "HOLD",
            "confidence": 0.0,
            "rationale": "Insufficient live bars for structure analysis yet."
        }
        base_hold = _apply_system_mode_to_signal(base_hold, symbol)
        response["signal"] = base_hold
        print(f"\n[REALTIME SIGNAL from POST] {symbol} {timeframe} → {base_hold.get('signal')} (not enough live bars yet)")

    # 4. Store to DB in background (after we already responded to MT5) - now using pool
    def _persist_to_db():
        from app.db import db_cursor
        try:
            with db_cursor() as (c, cur):
                ts_val = data.get("timestamp")
                if ts_val:
                    insert_query = """
                    INSERT INTO market_data
                    (symbol, timeframe, timestamp, open, high, low, close, tick_volume, spread)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """
                    cur.execute(insert_query, (
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
                    cur.execute(insert_query, (
                        symbol,
                        timeframe,
                        data.get("open"),
                        data.get("high"),
                        data.get("low"),
                        data.get("close"),
                        data.get("volume"),
                        data.get("spread", 0)
                    ))
                c.commit()
        except Exception as e:
            logger.error(f"Background DB insert failed for {symbol}: {e}")

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
        "note": "Number of recent M1 bars kept in memory (buffer caps at 1w+4d=15840 before rewrite). Post-off trend/structure uses only 1440 (1 day) from market. Direct from market."
    }


@app.get("/debug/live-buffer/{symbol}")
def debug_live_buffer_symbol(symbol: str):
    """Returns detailed information about the live buffer for one symbol."""
    info = live_buffer.get_buffer_status(symbol)
    info["note"] = "Recent live data from market buffer (grows to 1w+4d=15840 before rewrite). After off, only 1440 for trend/structure. Direct from market, no DB."
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
    """Current open trades for dashboard live view. (now pooled) + management suggestions"""
    from app.db import ensure_live_tables, db_cursor
    from app.features.builder import compute_structure
    ensure_live_tables()
    try:
        sym = normalize_symbol(symbol) if symbol else None
        base = """
            SELECT ts, symbol, direction, entry_price, stop_loss, tp1, tp2, volume_lots, notes, ticket, outcome, entry_context, setup
            FROM executed_trades
            WHERE (outcome = 'open' OR outcome IS NULL OR outcome = '')
        """
        if sym:
            sym_clause, sym_params = symbol_sql_match(sym)
            q = f"{base} AND {sym_clause} ORDER BY ts DESC LIMIT %s"
            params = sym_params + (limit,)
        else:
            q = base + " ORDER BY ts DESC LIMIT %s"
            params = (limit,)
        with db_cursor() as (c, cur):
            try:
                c.rollback()
            except:
                pass
            cur.execute(q, params)
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            out = []
            for row in rows:
                d = dict(zip(cols, row))
                if d.get("ts") and hasattr(d.get("ts"), "isoformat"):
                    d["ts"] = d["ts"].isoformat()
                out.append(d)

        # Attach management suggestions using current live structures
        if out:
            live_mss = {}
            for t in out:
                s = t.get("symbol")
                if s and s not in live_mss:
                    try:
                        from app.live_data import filter_df_to_catchup_window
                        df = live_buffer.get_recent_m5_df(s, limit=200)
                        df = filter_df_to_catchup_window(df)
                        if df is not None and len(df) >= 5:
                            ms = compute_structure(df, symbol=s, timeframe="M5", min_candles=5)
                            live_mss[s] = ms
                    except:
                        pass
            managements = compute_managements_for_all_opens(out, live_mss, get_system_mode())
            mgmt_by_ticket = {m["ticket"]: m for m in managements}
            for t in out:
                tkt = t.get("ticket")
                if tkt in mgmt_by_ticket:
                    t["management"] = mgmt_by_ticket[tkt]
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
        "mode": get_system_mode(),
        "tracked": syms,
        "buffers_ok": buffers_ok,
        "note": "Buffer: 1 week (10080) + 4 days headroom (15840 total) before rewrite. Post-off: trend/structure only 1440 (1 day) from market/backfill. Direct from market for displays."
    }

@app.get("/api/system-status")
def api_system_status():
    syms = [s for s in live_buffer.list_tracked_symbols() if s in ("XAUUSD", "DE30", "USTEC", "US30")]
    buffers = {s: live_buffer.get_buffer_status(s) for s in syms}
    return {
        "status": "running",
        "version": "post-fix",
        "timestamp": datetime.utcnow().isoformat(),
        "mode": get_system_mode(),
        "buffer_max_m1": getattr(live_buffer, 'max_bars', 15840),
        "buffer_max_m5": getattr(live_buffer, 'max_m5_bars', 3168),
        "catchup_max_hours": get_settings().catchup_max_hours,
        "catchup_max_m1_bars": get_settings().catchup_max_m1_bars,
        "symbols_tracked": syms,
        "buffers": buffers,
        "recent_data_quality_issues": {s: get_quality_issues().get(s, []) for s in syms},
        "alerts": _compute_current_alerts(),
    }

@app.get("/metrics")
def metrics():
    """Basic prometheus-style text metrics (no external deps)."""
    lines = []
    mode = get_system_mode()
    lines.append(f'capri_system_mode{{mode="{mode}"}} 1')
    try:
        syms = live_buffer.list_tracked_symbols()
        for s in syms:
            st = live_buffer.get_buffer_status(s)
            lines.append(f'capri_buffer_bars{{symbol="{s}"}} {st.get("bars_in_buffer", 0)}')
            lines.append(f'capri_buffer_m5_bars{{symbol="{s}"}} {st.get("m5_bars_in_buffer", 0)}')
    except:
        pass
    # quality issues count
    for s, bads in list(get_quality_issues().items())[:10]:
        lines.append(f'capri_data_quality_bad_count{{symbol="{s}"}} {len(bads)}')
    # simple health
    lines.append(f'capri_up 1')
    return "\n".join(lines) + "\n"

@app.get("/api/alerts")
def api_alerts():
    """Dedicated alerts endpoint for UI / monitoring (kill, streak, daily loss, quality)."""
    return {"alerts": _compute_current_alerts(), "timestamp": datetime.utcnow().isoformat()}

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
        from app.live_data import filter_df_to_catchup_window
        df_m5 = filter_df_to_catchup_window(df_m5)
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
    from app.db import ensure_live_tables, db_cursor
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
            params = sym_params + (limit,)
        else:
            q = """
                SELECT ts, symbol, timeframe, signal, score, confidence, setup, rationale,
                       structure_summary, bias, current_price, buffer_bars, raw_response
                FROM live_signals
                ORDER BY ts DESC
                LIMIT %s
            """
            params = (limit,)
        with db_cursor() as (c, cur):
            try:
                c.rollback()
            except:
                pass
            cur.execute(q, params)
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
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
        return []

@app.get("/api/trades")
def api_trades(symbol: str = None, limit: int = 200):
    from app.db import ensure_live_tables, db_cursor
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
            params = sym_params + (limit,)
        else:
            q = """
                SELECT ts, symbol, direction, entry_price, stop_loss, tp1, tp2,
                       r_multiple, outcome, volume_lots, notes, setup
                FROM executed_trades
                ORDER BY ts DESC
                LIMIT %s
            """
            params = (limit,)
        with db_cursor() as (c, cur):
            try:
                c.rollback()
            except:
                pass
            cur.execute(q, params)
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            out = []
            for row in rows:
                d = dict(zip(cols, row))
                if d.get("ts") and hasattr(d["ts"], "isoformat"):
                    d["ts"] = d["ts"].isoformat()
                out.append(d)
            return out
    except Exception as e:
        print(f"api_trades error: {e}")
        return []


# =============================================================================
# KILL SWITCH / CONTROL ENDPOINTS + MODE AWARENESS (Phase 2)
# =============================================================================

@app.get("/api/system-mode")
def api_system_mode():
    """Current trading mode for UI/EA. Modes: trading | paused | flatten"""
    return {
        "mode": get_system_mode(),
        "timestamp": datetime.utcnow().isoformat(),
        "note": "trading=normal, paused=signals HOLD only, flatten=close all positions + HOLD"
    }

@app.post("/api/control")
def api_control(payload: dict):
    """
    Control endpoint for kill switch etc.
    payload: {"action": "flatten_all" | "pause" | "resume" | "set_mode"}
    If set_mode, include "mode": "trading"|"paused"|"flatten"
    """
    action = (payload.get("action") or "").lower()
    new_mode = payload.get("mode")

    if action == "flatten_all" or new_mode == "flatten":
        set_system_mode("flatten")
        logger.warning("[CONTROL] FLATTEN_ALL requested - all positions should be closed by EA")
        return {"status": "ok", "mode": "flatten", "message": "FLATTEN signal active. EA should close everything."}
    elif action == "pause" or new_mode == "paused":
        set_system_mode("paused")
        logger.warning("[CONTROL] PAUSE requested")
        return {"status": "ok", "mode": "paused", "message": "Trading paused. Signals will be HOLD."}
    elif action == "resume" or new_mode == "trading":
        set_system_mode("trading")
        logger.info("[CONTROL] RESUME to trading")
        return {"status": "ok", "mode": "trading", "message": "Normal trading resumed."}
    elif action == "set_mode" and new_mode in ("trading", "paused", "flatten"):
        set_system_mode(new_mode)
        return {"status": "ok", "mode": new_mode}
    else:
        return {"status": "error", "message": "Unknown action. Use flatten_all, pause, resume, or set_mode + mode."}

# System mode helpers now live in app/system_mode.py (imported above).
# This eliminates duplication and the previous circular import hacks.
