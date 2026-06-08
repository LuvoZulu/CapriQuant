import pandas as pd
import json
from fastapi import APIRouter, HTTPException, Query
from app.features.builder import compute_structure, get_enriched_features
# compute_features (with MACD/RSI/EMA etc.) is deliberately NOT imported/used for any structure/crt/session_amd/risk paths.
# It remains only for legacy backtest experiments if explicitly opted into elsewhere.
from app.consensus import get_signal as legacy_get_signal
from app.engine.confluence import get_structure_signal
from app.engine.multi_timeframe import get_mtf_structure_signal
from app.utils.signal_logger import log_signal
from app.risk.risk_manager import get_risk_manager
# (removed legacy RiskParams + db streak queries here; risk execution unified to singleton / World path)
from app.config import get_settings
from app.utils.symbols import normalize_symbol as _normalize_symbol

# System mode (kill switch) from shared module (no circular hacks)
from app.system_mode import get_system_mode, _apply_system_mode_to_signal

router = APIRouter()

CANDLE_LIMIT = 200


def _resolve_validated_stop(signal: dict) -> float | None:
    """Pick a structural stop for the EA — never use current_price as SL."""
    if not isinstance(signal, dict):
        return None
    for key in ("validated_stop", "stop_suggestion", "stop"):
        val = signal.get(key)
        if val and float(val) > 0:
            return float(val)
    ms = signal.get("market_structure")
    if isinstance(ms, dict):
        val = ms.get("stop_suggestion")
        if val and float(val) > 0:
            return float(val)
    return None
MIN_CANDLES_FOR_SIGNAL = 50  # You can lower this for testing if needed


# Use canonical normalizer (and re-export for any local consumers)
def normalize_symbol(symbol: str) -> str:
    return _normalize_symbol(symbol)


def fetch_candles(conn, symbol: str, timeframe: str, engine: str = "legacy", min_candles_override: int = None) -> pd.DataFrame:
    normalized_symbol = normalize_symbol(symbol)

    query = """
        SELECT timestamp, open, high, low, close, tick_volume as volume
        FROM market_data
        WHERE symbol = %s AND timeframe = %s
        ORDER BY timestamp DESC
        LIMIT %s
    """
    # Prefer pooled cursor if a raw conn was passed; fall back
    from app.db import db_cursor
    try:
        with db_cursor() as (c, cur):
            cur.execute(query, (normalized_symbol, timeframe, CANDLE_LIMIT))
            rows = cur.fetchall()
    except Exception:
        # legacy fallback
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
    """Debug endpoint — live M1 buffer counts (EA stream only, no historical backfill)."""
    from app.live_data import get_buffer_status, get_all_buffer_lengths, list_tracked_symbols, get_recent_df
    try:
        if symbol:
            normalized = normalize_symbol(symbol)
            status = get_buffer_status(normalized)
            count = status.get("bars_in_buffer", 0)
            # For requested tf, we can note it's M1 based, or resample count but keep simple: report M1 live from market
            return {
                "normalized_symbol": normalized,
                "timeframe": (timeframe or "M1").upper(),
                "candle_count": count,
                "ready_for_default_structure": count >= 30,
                "ready_for_min_8 (what your EA uses)": count >= 8,
                "note": "Live stream only — buffer grows from EA attach; cap from EA buffer_max_m1.",
                "buffer_status": status,
                "source": "live_market_buffer"
            }
        else:
            lengths = get_all_buffer_lengths()
            tracked = list_tracked_symbols()
            return {
                "all_live_market_buffers": lengths,
                "tracked": tracked,
                "note": "Live M1 counts from in-memory buffer (EA-driven cap).",
                "default_buffer_cap": get_settings().default_buffer_max_m1,
                "source": "live_market_buffer"
            }
    except Exception as e:
        return {"error": str(e)}


@router.get("/api/current-structure/{symbol}")
def get_current_structure(symbol: str):
    """
    Snapshot for UI Live Overview symbol cards.
    Returns live buffer counts (M1 + derived M5) + key structure fields so the
    dashboard can render updating M1/M5 candle progress bars, readiness, bias, OBs, swings.
    This was missing, causing UI candle counts to stay at 0 / 'insufficient'.
    """
    normalized = normalize_symbol(symbol)
    try:
        from app.live_data import get_buffer_status, get_latest_price
        buf = get_buffer_status(normalized)
        live_p = get_latest_price(normalized)
        current_price = live_p.get("close") if live_p else None
    except Exception as e:
        buf = {"bars_in_buffer": 0, "m5_bars_in_buffer": 0, "pct_full": 0, "m5_pct_full": 0}
        current_price = None

    # Attempt a lightweight MTF snapshot for bias/summary/obs counts + full richness (non-fatal)
    bias = "NEUTRAL"
    summary = "Awaiting live bars..."
    bull_obs = 0
    bear_obs = 0
    swings = 0
    structure_richness = None
    try:
        from app.engine.multi_timeframe import get_mtf_structure_signal
        res = get_mtf_structure_signal(
            normalized, account_equity=0.0, spread=0.0
        ) or {}
        if isinstance(res, dict):
            bias = res.get("bias") or bias
            summary = res.get("structure_summary") or res.get("rationale") or summary
            ms = res.get("market_structure") or {}
            bull_obs = ms.get("active_bullish_obs", res.get("active_bullish_obs", 0)) or 0
            bear_obs = ms.get("active_bearish_obs", res.get("active_bearish_obs", 0)) or 0
            swings = ms.get("swing_count", res.get("swing_count", 0)) or 0
            if current_price is None:
                current_price = res.get("current_price")
            # Pass the rich structure details we now stamp (unfilled FVGs, obs counts, manipulation, breaks, etc.)
            structure_richness = res.get("structure_richness")
    except Exception:
        # Fall back to buffer-driven status only; UI will still show correct M1/M5 counts
        if buf.get("m5_bars_in_buffer", 0) >= 8:
            summary = "Live M5 context building"
        elif buf.get("bars_in_buffer", 0) >= 5:
            summary = "Collecting M1 bars for M5 resample"

    status = "ok"
    if buf.get("bars_in_buffer", 0) < 5:
        status = "insufficient_live_data"

    payload = {
        "symbol": normalized,
        "buffer": buf,
        "current_price": current_price,
        "bias": bias,
        "structure_summary": summary,
        "active_bullish_obs": bull_obs,
        "active_bearish_obs": bear_obs,
        "swing_count": swings,
        "status": status,
    }
    if structure_richness:
        payload["structure_richness"] = structure_richness
    return payload


@router.get("/debug/live-buffer")
def debug_live_buffer():
    """Debug endpoint used by dashboard 'Live Buffer Health' section."""
    try:
        from app.live_data import list_tracked_symbols, get_all_buffer_lengths, get_buffer_status
        tracked = list_tracked_symbols()
        return {
            "tracked": tracked,
            "lengths": get_all_buffer_lengths(),
            "per_symbol": {s: get_buffer_status(s) for s in tracked},
            "source": "live_market_buffer",
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/signal/{symbol}/{timeframe}")
def get_trading_signal(
    symbol: str,
    timeframe: str,
    spread: float = 0.0,
    engine: str = Query("structure", description="structure (now defaults to MTF precision) | mtf | structure_mtf | structure_single (old single-TF) | legacy"),
    min_candles: int = Query(
        None, 
        description="Temporarily lower the minimum candles needed (e.g. ?min_candles=10). Only for Strategy Tester / testing."
    ),
    equity: float = Query(0.0, description="Current account equity for risk sizing and circuits (from EA)."),
):
    from app.db import conn

    normalized = normalize_symbol(symbol)
    tf_upper = timeframe.upper()

    # === Graceful insufficient-data handling (eliminates 400 spam) ===
    # Very common when data-feeder EAs and signal consumers start at the same time.
    # Return clean 200 + HOLD instead of hard 400.
    from app.live_data import get_buffer_status, get_min_candles_m1
    min_required = min_candles if min_candles is not None else get_min_candles_m1(normalized)
    if engine != "structure":
        min_required = max(min_required, MIN_CANDLES_FOR_SIGNAL)
    min_required = max(min_required, 5)

    # Live buffer only — no DB / historical fallback
    candles_available = 0
    effective_closed_m1 = 0
    try:
        buf_status = get_buffer_status(normalized)
        raw = buf_status.get("bars_in_buffer", 0)
        # Effective for decisions: exclude the current forming bar (last one is usually still updating)
        effective_closed_m1 = max(0, raw - 1)
        # But for brand new / backfill-heavy buffers, if we have many we can be more generous
        candles_available = raw if raw >= 8 else effective_closed_m1
    except Exception as e:
        print(f"[SIGNALS] live buffer count failed for {normalized}: {e}")
        candles_available = 0

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
        # Still respect kill switch even on insufficient data path
        response_body = _apply_system_mode_to_signal(response_body)
        return response_body

    # Strongly prefer live aggregated data for real-time structure decisions
    # Use closed bars (no forming minute) for accurate structure (timestamp + accuracy fix)
    from app.live_data import get_recent_df_for_structure, get_latest_price
    live_df = get_recent_df_for_structure(normalized, limit=200)

    if live_df is None or len(live_df) < min_required:
        friendly = {
            "signal": "HOLD",
            "score": 0.0,
            "confidence": 0.0,
            "engine": engine,
            "setup": None,
            "rationale": (
                f"Insufficient live market data for {normalized}. "
                f"Only {candles_available} M1 bars since EA attach (need ≥ {min_required})."
            ),
            "candles_available": candles_available,
            "min_required": min_required,
        }
        response_body = {"symbol": normalized, "timeframe": tf_upper, "engine": engine, **friendly}
        return _apply_system_mode_to_signal(response_body)

    df = live_df
    live_price = get_latest_price(normalized)
    if live_price and len(df) > 0:
        df.loc[df.index[-1], 'close'] = live_price['close']
        if 'high' in df.columns:
            df.loc[df.index[-1], 'high'] = max(df.loc[df.index[-1], 'high'], live_price['close'])
        if 'low' in df.columns:
            df.loc[df.index[-1], 'low'] = min(df.loc[df.index[-1], 'low'], live_price['close'])

    if engine in ("structure", "mtf", "structure_mtf"):
        # Default "structure" now prefers full MTF production path (M5 primary + M1 confirm + M15 veto) for best accuracy.
        # Single-TF fallback only if insufficient multi-TF closed bars.
        # Use ?engine=structure_single for legacy single-TF behavior if needed.
        use_mtf = engine in ("mtf", "structure_mtf") or engine == "structure"
        if use_mtf:
            result = get_mtf_structure_signal(normalized, spread=spread, account_equity=equity)
            if result is None or result.get("signal") == "HOLD" and "Building" in str(result.get("rationale", "")):
                # graceful fallback
                ms = compute_structure(df, symbol=normalized, timeframe=timeframe, min_candles=min_candles or 10)
                result = get_structure_signal(ms, spread)
                result["engine"] = "structure_fallback_single"
        else:
            ms = compute_structure(df, symbol=normalized, timeframe=timeframe, min_candles=min_candles or 10)
            result = get_structure_signal(ms, spread)
    else:
        # Strictly avoid MACD/RSI/EMA/legacy indicator soup for core evaluation.
        # Force structure path (compute_structure + get_structure_signal) even for other engines.
        # This ensures we only use structure.py (full), crt_strategy (via MTF), session_amd (via MTF),
        # and risk_manager.py. No crt.py, amd.py, risk.py, and no oscillator features for decisions.
        ms = compute_structure(df, symbol=normalized, timeframe=timeframe, min_candles=min_candles or 10)
        result = get_structure_signal(ms, spread)
        result["engine"] = (engine or "structure") + "_forced_structure"

    if engine in ("structure", "mtf", "structure_mtf"):
        try:
            log_signal(result, symbol=normalized, timeframe=tf_upper)
        except Exception:
            pass

    # =============================================================================
    # Risk via the production singleton (now the single source of truth).
    # The previous inline re-construction of RiskManager + direct DB streak/today_r
    # queries duplicated (and could contradict) the risk execution already performed
    # inside get_mtf_structure_signal / World.compute_signal for the live path.
    # We now delegate to the same singleton used by /market-data so risk decisions
    # are consistent regardless of which endpoint a consumer hits.
    # =============================================================================
    final_signal = result.get("signal", "HOLD") if isinstance(result, dict) else "HOLD"
    risk_info = {}
    if final_signal in ("BUY", "SELL"):
        try:
            rm = get_risk_manager()
            # The MTF/World path already ran full circuits + get_risk_pct + stop validation.
            # Here we only surface the current state for the response (no second veto).
            rs = rm.get_state_dict() if hasattr(rm, "get_state_dict") else {}
            risk_info = {
                "risk_source": "risk_manager_singleton",
                "risk_streak": rs.get("loss_streak", 0),
                "risk_is_halted": rs.get("is_halted", False),
            }
            if rs.get("is_halted"):
                final_signal = "HOLD"
                if isinstance(result, dict):
                    result["signal"] = "HOLD"
                    result["rationale"] = (result.get("rationale", "") + " | RiskManager halted: " + str(rs.get("halt_reason"))).strip()
            else:
                if isinstance(result, dict):
                    vstop = _resolve_validated_stop(result)
                    if vstop:
                        risk_info["validated_stop"] = vstop
                        result["validated_stop"] = vstop
        except Exception as e:
            print(f"[RISK] layer error (non-fatal): {e}")
            risk_info = {"risk_error": str(e)}

    response_body = {
        "symbol": normalized,
        "timeframe": tf_upper,
        "engine": engine,
        **result,
    }
    if risk_info:
        response_body.update(risk_info)
    if final_signal == "HOLD" and response_body.get("signal") != "HOLD":
        response_body["signal"] = "HOLD"

    # Apply system kill/pause mode as final hard layer (after risk)
    response_body = _apply_system_mode_to_signal(response_body)

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
