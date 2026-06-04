import pandas as pd
import json
from fastapi import APIRouter, HTTPException, Query
from app.features.builder import compute_features, compute_structure, get_enriched_features
from app.consensus import get_signal as legacy_get_signal
from app.engine.confluence import get_structure_signal
from app.engine.multi_timeframe import get_mtf_structure_signal
from app.utils.signal_logger import log_signal
from app.risk import RiskManager, RiskParams
from app.db import get_recent_loss_streak, get_today_realized_r
from app.config import get_settings

# For kill switch / system mode (shared with main)
# NOTE: lazy import inside funcs to avoid fragile top-level cross import (main <-> app.api.signals)
# which could cause dummy always-trading funcs and kill/pause ignored on /signal poll path from EA.
def get_system_mode():
    try:
        from main import get_system_mode as _gm
        return _gm()
    except Exception:
        return "trading"

def _apply_system_mode_to_signal(d, s=""):
    try:
        from main import _apply_system_mode_to_signal as _ap
        return _ap(d, s)
    except Exception:
        return d

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

    max_hours = float(get_settings().catchup_max_hours)
    query = """
        SELECT timestamp, open, high, low, close, tick_volume as volume
        FROM market_data
        WHERE symbol = %s AND timeframe = %s
          AND timestamp >= NOW() - (%s * INTERVAL '1 hour')
        ORDER BY timestamp DESC
        LIMIT %s
    """
    # Prefer pooled cursor if a raw conn was passed; fall back
    from app.db import db_cursor
    try:
        with db_cursor() as (c, cur):
            cur.execute(query, (normalized_symbol, timeframe, max_hours, CANDLE_LIMIT))
            rows = cur.fetchall()
    except Exception:
        # legacy fallback
        cursor = conn.cursor()
        cursor.execute(query, (normalized_symbol, timeframe, max_hours, CANDLE_LIMIT))
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
    """Debug endpoint to see how much data exists for a symbol/timeframe (pooled)"""
    from app.db import db_cursor
    try:
        with db_cursor() as (c, cursor):
            if symbol:
                normalized = normalize_symbol(symbol)
                if timeframe:
                    cursor.execute(
                        "SELECT COUNT(*) FROM market_data WHERE symbol = %s AND timeframe = %s",
                        (normalized, timeframe.upper())
                    )
                    count = cursor.fetchone()[0]
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
                    return {"normalized_symbol": normalized, "by_timeframe": dict(rows)}
            else:
                cursor.execute("SELECT symbol, timeframe, COUNT(*) FROM market_data GROUP BY symbol, timeframe ORDER BY symbol, timeframe")
                rows = cursor.fetchall()
                return {"all_data": [{"symbol": r[0], "timeframe": r[1], "count": r[2]} for r in rows]}
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
    default_min = 15 if engine == "structure" else MIN_CANDLES_FOR_SIGNAL   # lowered from 30 for live data bootstrapping
    min_required = min_candles if min_candles is not None else default_min
    min_required = max(min_required, 5)

    candles_available = 0
    from app.db import db_cursor
    try:
        with db_cursor() as (c, cursor):
            try:
                c.rollback()
            except:
                pass
            max_hours = float(get_settings().catchup_max_hours)
            cursor.execute(
                """
                SELECT COUNT(*) FROM market_data
                WHERE symbol = %s AND timeframe = %s
                  AND timestamp >= NOW() - (%s * INTERVAL '1 hour')
                """,
                (normalized, tf_upper, max_hours),
            )
            candles_available = cursor.fetchone()[0] or 0
    except Exception as e:
        print(f"[SIGNALS] count candles failed for {normalized}: {e}")
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
        response_body = _apply_system_mode_to_signal(response_body, normalized)
        return response_body

    # Strongly prefer live aggregated data for real-time structure decisions
    # Use closed bars (no forming minute) for accurate structure (timestamp + accuracy fix)
    from app.live_data import get_recent_df_for_structure, get_latest_price
    live_df = get_recent_df_for_structure(normalized, limit=200)

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

    if engine in ("structure", "mtf", "structure_mtf"):
        # Default "structure" now prefers full MTF production path (M5 primary + M1 confirm + M15 veto) for best accuracy.
        # Single-TF fallback only if insufficient multi-TF closed bars.
        # Use ?engine=structure_single for legacy single-TF behavior if needed.
        use_mtf = engine in ("mtf", "structure_mtf") or engine == "structure"
        if use_mtf:
            result = get_mtf_structure_signal(normalized, spread=spread, min_candles_m1=8, equity=equity)
            if result is None or result.get("signal") == "HOLD" and "Building" in str(result.get("rationale", "")):
                # graceful fallback
                ms = compute_structure(df, symbol=normalized, timeframe=timeframe, min_candles=min_candles or 10)
                result = get_structure_signal(ms, spread)
                result["engine"] = "structure_fallback_single"
        else:
            ms = compute_structure(df, symbol=normalized, timeframe=timeframe, min_candles=min_candles or 10)
            result = get_structure_signal(ms, spread)
    else:
        features = compute_features(df)
        result = legacy_get_signal(features, spread)

    if engine in ("structure", "mtf", "structure_mtf"):
        try:
            log_signal(result, symbol=normalized, timeframe=tf_upper)
        except Exception:
            pass

    # =============================================================================
    # HARD RiskManager layer (non-bypassable): live equity + streak + daily loss veto
    # Must run for every BUY/SELL decision. Turns risky signals into HOLD.
    # =============================================================================
    final_signal = result.get("signal", "HOLD") if isinstance(result, dict) else "HOLD"
    risk_info = {}
    if final_signal in ("BUY", "SELL"):
        try:
            eq = float(equity) if equity and equity > 1.0 else 200.0
            # Account-level risk (streak + daily loss circuits protect the whole account, not per-symbol)
            streak = get_recent_loss_streak(None) or 0
            today_r = get_today_realized_r(None) or 0.0
            # today_pnl proxy: realized r * rough risk amount (use 1.5% of current eq as avg)
            avg_risk_money = eq * 0.015
            today_pnl = today_r * avg_risk_money
            s = get_settings()
            params = RiskParams(
                account_equity=eq,
                starting_equity=s.risk_starting_equity,
                target_equity=s.risk_target_equity,
                max_daily_loss_pct=s.risk_max_daily_loss_pct,
                base_risk_pct=s.risk_base_pct,
                aggressive_risk_pct=s.risk_aggressive_pct,
                conservative_risk_pct=s.risk_conservative_pct,
            )
            rm = RiskManager(params)
            allowed, veto_reason, eff_risk_pct = rm.can_take_trade(
                recent_loss_streak=streak,
                today_pnl=today_pnl,
                starting_equity_today=eq,
            )
            risk_info = {
                "risk_pct": round(eff_risk_pct, 2),
                "risk_streak": streak,
                "risk_today_r": round(today_r, 2),
                "risk_veto": None if allowed else veto_reason,
            }
            if not allowed:
                final_signal = "HOLD"
                # update rationale
                old_rationale = result.get("rationale", "") if isinstance(result, dict) else ""
                new_rationale = f"Risk veto: {veto_reason} (streak={streak}, daily_r={today_r:.1f}). {old_rationale}".strip()
                if isinstance(result, dict):
                    result["signal"] = "HOLD"
                    result["rationale"] = new_rationale
                print(f"[RISK VETO] {normalized} {final_signal} <- was {result.get('signal','?')} : {veto_reason}")
            else:
                # attach validated stop if structure provided one (for EA to prefer)
                if isinstance(result, dict):
                    vstop = _resolve_validated_stop(result)
                    if vstop:
                        risk_info["validated_stop"] = vstop
                        result["validated_stop"] = vstop
        except Exception as e:
            print(f"[RISK] layer error (non-fatal, allowing original): {e}")
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
    response_body = _apply_system_mode_to_signal(response_body, normalized)

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
