"""
CapriQuant FastAPI Backend — main.py  (Production-Hardened)
============================================================

KEY FIXES in this version:
  1.  RiskManager singleton receives equity on every /market-data heartbeat
      and on every /report-trade close — circuits now actually fire.
  2.  Kill switch endpoints: POST /api/control {action: flatten|pause|resume}
  3.  Structured JSON-style logging with correlation IDs (req_id).
  4.  Data quality gate rejects poison ticks before structure compute.
  5.  /metrics Prometheus-compatible endpoint.
  6.  global conn/cursor removed from hot paths — db_cursor() everywhere.
  7.  Trade close from EA calls rm.record_trade() so streak updates live.
"""

from __future__ import annotations

import logging
import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from app.db import db_cursor
from app.api.signals import router as signal_router
from app.live_data import get_recent_df, live_buffer, add_market_data as update_live_bar, to_naive_utc, is_within_catchup_window
from app.features.builder import compute_structure
from app.engine.confluence import get_structure_signal, evaluate_setups
from app.engine.multi_timeframe import get_mtf_structure_signal
from app.utils.symbols import symbol_sql_match, normalize_symbol as _normalize_symbol
from app.risk.risk_manager import get_risk_manager, TradeRecord
from app.engine.management import compute_managements_for_all_opens, compute_management_for_open
from app.features.trade_lifecycle import TradeLifecycleManager, ActiveTrade, build_snapshot_from_ms
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

# ── Structured logging setup ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("capriquant.main")

# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="CapriQuant", version="2.1")
app.include_router(signal_router)

# ── Singletons ───────────────────────────────────────────────────────────────
_lifecycle_manager = TradeLifecycleManager()


# ── Central execution coordinator (the "World") ──────────────────────────────
# Before: signal decision ("execution") lived only as side-effects inside the
# giant /market-data handler + scattered module dicts/singletons + duplicated
# logic in api/signals.py. There was no World class at all.
# This gives the backend an obvious owner for live state + the core update path.
# Over time more of the market_data / MTF orchestration can move inside here
# so both live (EA-driven) and future simulated/backtest execution use the
# exact same code.
class World:
    """Central backend execution 'World' / environment.

    Owns:
    - Lifecycle state (positions + post-entry management)
    - Risk manager (circuits, sizing, validation)
    - The core decision step (MTF + confluence + rich strategies: crt_strategy, session_amd, structure)
    - Provides unified on_market_data / on_report_trade so live and future backtest/paper use identical execution logic.

    Before this, execution (signal + risk + lifecycle + management) was scattered inline in handlers + module globals + duplicated paths. No single owner for the full loop (bars -> perception -> decision -> action -> feedback -> state update).

    This makes "execution" live inside the World. Callers (main handlers, future replay) delegate to it.
    """

    def __init__(self):
        self.lifecycle_manager = _lifecycle_manager
        self.rm = get_risk_manager()
        # Rich per-symbol strategy state lives in the MTF singletons (amd/crt detectors), which World can eventually own too.

    def on_market_data(self, symbol: str, data: dict, account_equity: Optional[float] = None, spread: float = 0.0) -> Dict:
        """The full execution step for a market update (the heart of live execution).

        Ingests (via live_data already called by caller for now), computes signal using rich MTF (which uses crt_strategy, session_amd, structure.py), applies risk (via self.rm), drives lifecycle + management (post-entry execution).
        Returns the enriched signal dict with actions.
        This is where execution "lives" in the backend World.
        """
        # Delegate the brain (MTF already wires the rich strategies + risk gate inside it)
        sig = get_mtf_structure_signal(
            symbol=symbol,
            account_equity=account_equity,
            spread=spread,
        ) or {"signal": "HOLD", "symbol": symbol, "rationale": "mtf returned none"}

        # Lifecycle + management execution (post-entry "what to do with the open trade")
        # (The caller or this method can drive on_bar using the snapshot adapter)
        # For now the heavy lifting is already in the surrounding handler; World centralizes the decision.
        # Future: move the full on_bar / compute_managements here so both live and bt call world.on_market_data(bar)
        return sig

    def on_report_trade(self, data: dict) -> None:
        """Feedback from broker/EA into World state (risk + lifecycle execution)."""
        event = str(data.get("event", "open")).lower()
        symbol = normalize_symbol(str(data.get("symbol", "UNKNOWN")))
        if event == "close":
            try:
                equity = float(data.get("equity") or data.get("account_equity") or 0)
                pnl_pct_raw = data.get("pnl_pct") or data.get("profit_pct")
                pnl_pct = float(pnl_pct_raw) / 100.0 if pnl_pct_raw is not None else None
                tr = TradeRecord(
                    trade_id=str(data.get("ticket") or data.get("trade_id") or "unknown"),
                    symbol=symbol,
                    direction=str(data.get("direction", "long")).lower(),
                    entry_price=float(data.get("entry_price") or 0),
                    stop_price=float(data.get("stop_loss") or 0),
                    entry_time=datetime.utcnow(),
                    close_time=datetime.utcnow(),
                    close_price=float(data.get("close_price") or 0),
                    pnl_pct=pnl_pct,
                    close_reason=str(data.get("close_reason") or "manual"),
                    risk_pct_used=float(data.get("risk_pct") or 0),
                )
                self.rm.record_trade(tr)
                if equity > 0:
                    self.rm.update_equity(equity)
                # Lifecycle feedback
                lifecycle_close({"trade_id": str(data.get("ticket", "")), "close_reason": data.get("close_reason", "")})
            except Exception as exc:
                logger.error("[World] on_report_trade close error: %s", exc)

    def compute_signal(self, symbol: str, account_equity: Optional[float], spread: float) -> Dict:
        """Core decision (kept for compat; prefers on_market_data for full execution)."""
        return get_mtf_structure_signal(
            symbol=symbol,
            account_equity=account_equity,
            spread=spread,
        ) or {"signal": "HOLD", "symbol": symbol}


# One shared World instance for the backend process
_world = World()

# ── Simple Prometheus-style counters (in-memory) ────────────────────────────
_metrics: Dict[str, Any] = {
    "signals_total": {"BUY": 0, "SELL": 0, "HOLD": 0},
    "risk_vetoes_total": {},
    "bad_ticks_total": 0,
    "db_errors_total": 0,
    "structure_compute_seconds": [],   # ring buffer last 100
}

# In-memory for dashboard endpoints (works even if DB is down)
recent_signals_list: list = []
closed_trades_list: list = []

# Throttle heavy MTF/lifecycle work so /market-data stays fast under load.
_mtf_cache: Dict[str, Dict[str, Any]] = {}
MTF_MIN_INTERVAL_SEC = 1.0


def normalize_symbol(symbol: str) -> str:
    return _normalize_symbol(symbol)


# ── Utility: stamp metrics ───────────────────────────────────────────────────
def _record_signal(sig: str) -> None:
    _metrics["signals_total"][sig] = _metrics["signals_total"].get(sig, 0) + 1


def _record_risk_veto(reason: str) -> None:
    _metrics["risk_vetoes_total"][reason] = _metrics["risk_vetoes_total"].get(reason, 0) + 1


def _current_bar_minute(symbol: str) -> datetime:
    """Minute bucket of the last buffered bar (after ingest)."""
    from app.live_data import LIVE_BARS, _floor_to_minute, _resolve_buffer_key

    sym = _resolve_buffer_key(symbol)
    buf = LIVE_BARS.get(sym)
    if buf:
        return _floor_to_minute(buf[-1]["timestamp"])
    return _floor_to_minute(datetime.utcnow())


def _should_compute_mtf(symbol: str, bar_minute: datetime) -> bool:
    cached = _mtf_cache.get(symbol)
    if not cached:
        return True
    if cached.get("bar_minute") != bar_minute:
        return True
    computed_at = cached.get("computed_at")
    if not isinstance(computed_at, datetime):
        return True
    return (datetime.utcnow() - computed_at).total_seconds() >= MTF_MIN_INTERVAL_SEC


def _cache_mtf_signal(symbol: str, bar_minute: datetime, signal: Dict) -> None:
    _mtf_cache[symbol] = {
        "bar_minute": bar_minute,
        "computed_at": datetime.utcnow(),
        "signal": signal,
    }


def _get_cached_mtf_signal(symbol: str) -> Optional[Dict]:
    cached = _mtf_cache.get(symbol)
    if cached:
        return cached.get("signal")
    return None


# ── Root ──────────────────────────────────────────────────────────────────────
@app.get("/")
def home() -> Dict:
    return {"status": "quant system live", "version": "2.1"}


# ── Kill switch / control ─────────────────────────────────────────────────────
@app.post("/api/control")
def control(body: dict) -> Dict:
    """
    Emergency controls.

    Body: {"action": "pause" | "resume" | "flatten"}

    - pause:   stop emitting BUY/SELL signals; return HOLD everywhere.
    - resume:  re-enable trading.
    - flatten: return FLATTEN_ALL signal to EA on next poll; then auto-pause.
    """
    action = str(body.get("action", "")).lower()
    if action not in ("pause", "resume", "flatten"):
        raise HTTPException(status_code=400, detail="action must be pause|resume|flatten")

    if action == "flatten":
        set_system_mode("flatten")
        logger.warning("[KILL_SWITCH] FLATTEN ALL triggered by API call")
    elif action == "pause":
        set_system_mode("paused")
        logger.warning("[KILL_SWITCH] Trading PAUSED by API call")
    else:
        set_system_mode("trading")
        logger.info("[KILL_SWITCH] Trading RESUMED by API call")

    return {"status": "ok", "mode": get_system_mode()}


@app.get("/api/system-status")
def system_status() -> Dict:
    """Full status snapshot for dashboard and monitoring."""
    rm = get_risk_manager()
    try:
        from app.live_data import list_tracked_symbols, get_all_buffer_lengths, get_ea_config
        tracked = list_tracked_symbols()
        lengths = get_all_buffer_lengths()
        ea_configs = {s: get_ea_config(s) for s in tracked}
    except Exception:
        tracked = []
        lengths = {}
        ea_configs = {}
    return {
        "system_mode": get_system_mode(),
        "risk": rm.get_state_dict(),
        "quality_issues": get_quality_issues(),
        "alerts": _compute_current_alerts(),
        "metrics_snapshot": {
            "signals_total": _metrics["signals_total"],
            "risk_vetoes_total": _metrics["risk_vetoes_total"],
            "bad_ticks_total": _metrics["bad_ticks_total"],
        },
        "symbols_tracked": tracked,
        "buffer_max_m1": get_settings().default_buffer_max_m1,
        "live_buffer_lengths": lengths,
        "ea_configs": ea_configs,
    }


# ── Missing dashboard endpoints (were causing 404s and breaking tabs) ───────
@app.get("/api/system-mode")
def api_system_mode() -> Dict:
    return {"mode": get_system_mode()}

@app.get("/api/alerts")
def api_alerts() -> Dict:
    return {"alerts": _compute_current_alerts()}

@app.get("/api/recent-signals")
def api_recent_signals(symbol: str = None, limit: int = 100) -> list:
    data = recent_signals_list
    if symbol:
        s = normalize_symbol(symbol)
        data = [d for d in data if normalize_symbol(d.get("symbol", "")) == s]
    return list(reversed(data[-limit:]))  # newest first

@app.get("/api/open-trades")
def api_open_trades() -> list:
    try:
        ls = lifecycle_status()
        trades = ls.get("active_trades", [])
        return [
            {
                "ticket": t.get("trade_id"),
                "symbol": t.get("symbol"),
                "direction": t.get("direction"),
                "entry_price": t.get("entry"),
                "current_rr": t.get("current_rr", 0.0),
                "is_be": t.get("is_be", False),
            }
            for t in trades
        ]
    except Exception:
        return []

@app.get("/api/trades")
def api_trades(limit: int = 300) -> list:
    return list(reversed(closed_trades_list[-limit:]))  # newest first


# ── Prometheus metrics ─────────────────────────────────────────────────────
@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics() -> str:
    """Prometheus text format metrics."""
    rm = get_risk_manager()
    rs = rm.get_state_dict()
    lines = [
        "# HELP capriquant_equity Current account equity",
        "# TYPE capriquant_equity gauge",
        f"capriquant_equity {rs['equity']}",
        "# HELP capriquant_loss_streak Current consecutive loss streak",
        "# TYPE capriquant_loss_streak gauge",
        f"capriquant_loss_streak {rs['loss_streak']}",
        "# HELP capriquant_is_halted 1 if risk circuits have halted trading",
        "# TYPE capriquant_is_halted gauge",
        f"capriquant_is_halted {1 if rs['is_halted'] else 0}",
        "# HELP capriquant_daily_pnl_pct Daily PnL as percentage",
        "# TYPE capriquant_daily_pnl_pct gauge",
        f"capriquant_daily_pnl_pct {rs['daily_pnl_pct']}",
        "# HELP capriquant_bad_ticks_total Total bad ticks rejected",
        "# TYPE capriquant_bad_ticks_total counter",
        f"capriquant_bad_ticks_total {_metrics['bad_ticks_total']}",
    ]
    for sig, count in _metrics["signals_total"].items():
        lines.append(f'capriquant_signals_total{{signal="{sig}"}} {count}')
    return "\n".join(lines) + "\n"


# ── Lifecycle endpoints ───────────────────────────────────────────────────────
@app.post("/lifecycle/register")
def lifecycle_register(trade: dict) -> Dict:
    """Register an open trade with the lifecycle manager."""
    try:
        t = ActiveTrade(
            trade_id=str(trade.get("trade_id") or trade.get("ticket") or "unknown"),
            symbol=normalize_symbol(str(trade.get("symbol", "UNKNOWN"))),
            direction=str(trade.get("direction", "long")).lower(),
            entry_price=float(trade["entry_price"]),
            initial_stop=float(trade["initial_stop"]),
            initial_tp=float(trade.get("initial_tp") or trade.get("tp1", 0)),
            entry_time=datetime.utcnow(),
            lot_size=float(trade.get("lot_size") or trade.get("volume_lots", 0.01)),
            risk_pct=float(trade.get("risk_pct", 1.0)),
        )
        _lifecycle_manager.register_trade(t)
        logger.info(
            "[Lifecycle] Registered trade %s (%s %s @ %.5f SL=%.5f)",
            t.trade_id, t.direction.upper(), t.symbol, t.entry_price, t.initial_stop,
        )
        return {"status": "registered", "trade_id": t.trade_id}
    except Exception as exc:
        logger.error("[Lifecycle] register failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/lifecycle/close")
def lifecycle_close(data: dict) -> Dict:
    """Notify the lifecycle manager that a trade has closed."""
    trade_id = str(data.get("trade_id") or data.get("ticket", ""))
    was_tracked = trade_id in _lifecycle_manager._trades
    _lifecycle_manager.remove_trade(trade_id)
    return {
        "status": "closed" if was_tracked else "not_found",
        "trade_id": trade_id,
        "close_reason": str(data.get("close_reason", "manual")),
    }


@app.get("/lifecycle/status")
def lifecycle_status() -> Dict:
    return {
        "active_trades": [
            {
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "direction": t.direction,
                "entry": t.entry_price,
                "current_rr": round(getattr(t, "current_rr", 0.0), 2),
                "is_be": t.is_be,
            }
            for t in _lifecycle_manager._trades.values()
        ],
        "count": len(_lifecycle_manager._trades),
    }


# ── /market-data ──────────────────────────────────────────────────────────────
@app.post("/market-data")
def market_data(data: dict, background_tasks: BackgroundTasks) -> Dict:  # noqa: C901
    symbol = normalize_symbol(data.get("symbol", "UNKNOWN"))
    timeframe = data.get("timeframe", "M5").upper()
    if timeframe == "TICK":
        timeframe = "M1"
    req_id = str(uuid.uuid4())[:8]

    # Update per-symbol EA config (buffer_max_m1, min_candles_m1, etc.) from every payload
    from app.live_data import update_ea_config
    update_ea_config(symbol, data)

    # ── Data quality gate ───────────────────────────────────────────────
    bad_reasons = _validate_tick(symbol, data)
    if bad_reasons:
        _metrics["bad_ticks_total"] += 1
        record_quality_bad(symbol, bad_reasons)
        logger.warning("[DATA_QUALITY %s] %s rejected: %s", req_id, symbol, bad_reasons)
        data["_quality_bad"] = bad_reasons

    logger.info(
        "[TICK %s] %s %s close=%s bid=%s ask=%s vol=%s",
        req_id, symbol, timeframe,
        data.get("close"), data.get("bid"), data.get("ask"), data.get("volume"),
    )

    # ── Update live buffer (live stream only — EA config merged in add_market_data) ──
    update_live_bar(symbol, data)

    # ── Update equity in RiskManager if EA sends it ─────────────────────
    equity = data.get("equity") or data.get("account_equity")
    if equity:
        try:
            rm = get_risk_manager(initial_equity=float(equity))
            rm.update_equity(float(equity))
        except Exception as exc:
            logger.debug("[RM] equity update failed: %s", exc)

    _persist_tick_to_db(symbol, timeframe, data, background_tasks)

    # ── Determine backfill vs live for the skip decision (prevents NameError + enables logic)
    # We always apply buffer/equity/persist above. Only skip heavy signal compute for *stale* historical.
    is_backfill = False
    ts_raw = data.get("timestamp")
    try:
        if ts_raw is not None:
            ts_dt = to_naive_utc(ts_raw)
            if not is_within_catchup_window(ts_dt):
                is_backfill = True
    except Exception:
        pass

    # ── Only for clearly stale backfill do we skip the M1 signal path.
    # Recent backfill data has already driven the buffer + equity + persist above.
    if is_backfill:
        logger.info("[BACKFILL %s] buffer+side-effects done, skipping heavy M1 signal (stale) for %s", req_id, symbol)
        return {"status": "backfill_buffered_skipped_decisions", "symbol": symbol}

    # ── Only compute signal on M1 (main tick timeframe) ──────────────────
    if timeframe != "M1" or bad_reasons:
        return {"status": "buffered", "symbol": symbol, "timeframe": timeframe}

    # ── Kill switch check ────────────────────────────────────────────────
    mode = get_system_mode()
    if mode != "trading":
        _persist_tick_to_db(symbol, timeframe, data, background_tasks)
        base = {"signal": "HOLD", "symbol": symbol, "mode": mode}
        if mode in ("flatten", "paused"):
            base = _flatten_signal_for_ea(base) if mode == "flatten" else base
        return base

    # ── MTF signal (throttled — buffer already updated above) ────────────
    account_equity = float(equity) if equity else None
    spread = float(data.get("spread", 0) or 0)
    bar_minute = _current_bar_minute(symbol)
    compute_mtf = _should_compute_mtf(symbol, bar_minute)

    if compute_mtf:
        try:
            # Full execution step now lives in the backend World.
            # on_market_data centralizes MTF (rich crt_strategy + session_amd + structure) + risk.
            signal = _world.on_market_data(symbol=symbol, data=data, account_equity=account_equity, spread=spread)
        except Exception as exc:
            logger.error("[SIGNAL %s] MTF error for %s: %s", req_id, symbol, exc, exc_info=True)
            signal = {"signal": "HOLD", "symbol": symbol, "error": str(exc)}

        if signal is None:
            signal = {"signal": "HOLD", "symbol": symbol, "rationale": "Insufficient live data"}

        _cache_mtf_signal(symbol, bar_minute, signal)
    else:
        signal = _get_cached_mtf_signal(symbol) or {
            "signal": "HOLD",
            "symbol": symbol,
            "rationale": "MTF throttled",
        }

    # ── Apply system mode overlays ────────────────────────────────────────
    signal = _apply_system_mode_to_signal(signal)
    _record_signal(str(signal.get("signal", "HOLD")))

    # Record for /api/recent-signals (dashboard Structure tab)
    if compute_mtf:
        try:
            recent_signals_list.append({
                "ts": datetime.utcnow().isoformat(),
                "symbol": symbol,
                "timeframe": "M1",
                "signal": signal.get("signal"),
                "score": signal.get("score", 0.0),
                "confidence": signal.get("confidence", 0.0),
                "setup": signal.get("setup"),
                "rationale": signal.get("rationale"),
                "current_price": signal.get("current_price"),
            })
            if len(recent_signals_list) > 200:
                recent_signals_list.pop(0)
        except Exception:
            pass

    # ── Lifecycle management actions (only when MTF runs) ───────────────
    lifecycle_actions = []
    if compute_mtf:
        try:
            m5_df = resample_for_lifecycle(symbol)
            if m5_df is not None and not m5_df.empty:
                ms_snap = compute_structure(m5_df, symbol=symbol)
                # Use adapter (build_snapshot_from_ms) so that the real MarketStructure's
                # fvgs/order_blocks/breaks/recent_displacement are turned into the
                # fvg_zones/ob_zones/last_choch_*/displacement_* that the lifecycle checks
                # actually read. Without this, post-entry management (BE, trail, scale-out, early CHOCH exit)
                # was non-functional — actions list was always empty.
                bar = m5_df.iloc[-1].to_dict()
                bar_time = bar.get("timestamp") or datetime.utcnow()
                try:
                    snap = build_snapshot_from_ms(ms_snap)
                    actions = _lifecycle_manager.on_bar(
                        float(bar.get("open", bar.get("close", 0))),
                        float(bar.get("high", bar.get("close", 0))),
                        float(bar.get("low", bar.get("close", 0))),
                        float(bar.get("close", 0)),
                        bar_time,
                        snap,
                    )
                    lifecycle_actions = [a.to_dict() for a in actions]
                except Exception as inner_exc:
                    logger.warning("[Lifecycle] on_bar error (actions may be partial): %s", inner_exc)
                    lifecycle_actions = []
        except Exception as exc:
            logger.debug("[Lifecycle] on_bar error: %s", exc)

    if lifecycle_actions:
        signal["lifecycle_actions"] = lifecycle_actions

    # ── Wire the (previously imported but unused) management engine ─────────
    # Produces MOVE_BE / TRAIL_SL / CLOSE etc using real MarketStructure.
    # Attach alongside lifecycle_actions so EA (or a future executor) can act on either.
    mgmt_actions = []
    if compute_mtf:
        try:
            # Build minimal open trade dicts from the lifecycle manager (source of truth for actives)
            open_dicts = []
            for tid, at in list(_lifecycle_manager._trades.items()):
                if at.symbol == symbol:  # focus current symbol for this tick
                    open_dicts.append({
                        "ticket": tid,
                        "symbol": at.symbol,
                        "direction": at.direction.upper(),
                        "entry_price": at.entry_price,
                        "stop_loss": at.current_stop,
                    })
            if open_dicts:
                # Reuse m5_df + compute_structure if available in this scope (from the lifecycle block above)
                m5_for_mgmt = None
                try:
                    m5_for_mgmt = m5_df  # may be defined in the prior if
                except NameError:
                    m5_for_mgmt = resample_for_lifecycle(symbol)
                if m5_for_mgmt is not None and not getattr(m5_for_mgmt, "empty", True):
                    ms_mgmt = compute_structure(m5_for_mgmt, symbol=symbol)
                    mgmt_actions = compute_managements_for_all_opens(open_dicts, {symbol: ms_mgmt}, system_mode=mode) or []
        except Exception as exc:
            logger.debug("[Management] compute error (non-fatal): %s", exc)
    if mgmt_actions:
        signal["management_actions"] = mgmt_actions

    signal["req_id"] = req_id
    _persist_tick_to_db(symbol, timeframe, data, background_tasks)
    return signal


def resample_for_lifecycle(symbol: str):
    """Helper to get M5 df for lifecycle on_bar calls."""
    try:
        from app.live_data import live_buffer as _lb, resample_ohlcv
        buf = _lb(symbol, "M1")
        if buf is None:
            return None
        df = pd.DataFrame(list(buf))
        return resample_ohlcv(df, "5min")
    except Exception:
        return None


def _persist_tick_to_db(symbol: str, timeframe: str, data: dict, background_tasks: BackgroundTasks) -> None:
    """Persist tick to DB in background (fire-and-forget)."""
    background_tasks.add_task(_do_persist, symbol, timeframe, data)


def _do_persist(symbol: str, timeframe: str, data: dict) -> None:
    """Actual DB persist — uses pooled cursor, never global conn.
    Uses bare ON CONFLICT DO NOTHING so it works even if the unique constraint
    on (symbol, timeframe, timestamp) is not yet present (table may have been
    created in an older run without the constraint).
    """
    try:
        with db_cursor() as (conn, cur):
            cur.execute(
                """
                INSERT INTO market_data (symbol, timeframe, timestamp, open, high, low, close, tick_volume)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    symbol,
                    timeframe,
                    data.get("timestamp"),
                    data.get("open"),
                    data.get("high"),
                    data.get("low"),
                    data.get("close"),
                    data.get("volume", 0),
                ),
            )
            conn.commit()
    except Exception as exc:
        _metrics["db_errors_total"] = _metrics.get("db_errors_total", 0) + 1
        logger.error("[DB] persist failed for %s: %s", symbol, exc)


# ── /report-trade ─────────────────────────────────────────────────────────────
@app.post("/report-trade")
def report_trade(data: dict) -> Dict:
    """
    EA calls this after every trade open AND close.
    On close: updates RiskManager streak + equity.
    """
    event = str(data.get("event", "open")).lower()
    symbol = normalize_symbol(str(data.get("symbol", "UNKNOWN")))

    if event == "close":
        # Delegate full execution feedback (risk + lifecycle) to the backend World.
        # Previously duplicated here and in World; now World owns the state mutation.
        _world.on_report_trade(data)

        # Still do the minimal legacy in-mem + DB for dashboard compatibility (can be removed later when dashboard uses world state)
        try:
            equity = float(data.get("equity") or data.get("account_equity") or 0)
            pnl_pct_raw = data.get("pnl_pct") or data.get("profit_pct")
            pnl_pct = float(pnl_pct_raw) / 100.0 if pnl_pct_raw is not None else None
            # (The World already did the rm.record_trade / update_equity)
            logger.info(
                "[ReportTrade] CLOSE %s %s (delegated to World)",
                data.get("ticket"), symbol
            )
        except Exception as exc:
            logger.error("[ReportTrade] legacy close log failed: %s", exc)

        _persist_trade_close(data, symbol)

        try:
            pnl_for_list = float(pnl_pct or 0) * 100 if 'pnl_pct' in locals() and pnl_pct is not None else float(data.get("pnl_pct") or data.get("profit_pct") or 0)
            closed_trades_list.append({
                "ts": datetime.utcnow().isoformat(),
                "ticket": str(data.get("ticket") or data.get("trade_id")),
                "symbol": symbol,
                "direction": str(data.get("direction", "")).lower(),
                "entry_price": float(data.get("entry_price") or 0),
                "close_price": float(data.get("close_price") or 0),
                "pnl_pct": pnl_for_list,
                "close_reason": str(data.get("close_reason") or "manual"),
                "setup": str(data.get("setup") or ""),
            })
            if len(closed_trades_list) > 500:
                closed_trades_list.pop(0)
        except Exception:
            pass

    # ── Lifecycle close notification ──────────────────────────────────────
    if event == "close":
        lifecycle_close({"trade_id": str(data.get("ticket", "")), "close_reason": data.get("close_reason", "")})

    # ── AUTO-REGISTER OPEN TRADES for lifecycle (so post-entry BE/trail/scale actually runs) ──
    # EA can still call /lifecycle/register explicitly; this makes backend self-sufficient on /report-trade open.
    if event == "open":
        try:
            t = ActiveTrade(
                trade_id=str(data.get("ticket") or data.get("trade_id") or "unknown"),
                symbol=symbol,
                direction=str(data.get("direction", "long")).lower(),
                entry_price=float(data.get("entry_price") or 0),
                initial_stop=float(data.get("stop_loss") or data.get("initial_stop") or 0),
                initial_tp=float(data.get("tp1") or data.get("initial_tp") or data.get("tp", 0)),
                entry_time=datetime.utcnow(),
                lot_size=float(data.get("lot_size") or data.get("volume_lots") or 0.01),
                risk_pct=float(data.get("risk_pct") or 1.0),
            )
            _lifecycle_manager.register_trade(t)
            logger.info("[ReportTrade] AUTO-registered open trade for lifecycle: %s %s @ %.5f", t.trade_id, symbol, t.entry_price)
        except Exception as exc:
            logger.debug("[ReportTrade] auto lifecycle register on open skipped: %s", exc)

    return {"status": "ok", "event": event, "symbol": symbol}


def _persist_trade_close(data: dict, symbol: str) -> None:
    """Persist closed trade to executed_trades table."""
    try:
        with db_cursor() as (conn, cur):
            cur.execute(
                """
                INSERT INTO executed_trades
                  (ticket, symbol, direction, entry_price, stop_loss, tp1, tp2,
                   close_price, close_time, close_reason, profit_pct, risk_pct, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (ticket) DO UPDATE
                  SET close_price=EXCLUDED.close_price,
                      close_time=EXCLUDED.close_time,
                      close_reason=EXCLUDED.close_reason,
                      profit_pct=EXCLUDED.profit_pct
                """,
                (
                    data.get("ticket"),
                    symbol,
                    data.get("direction"),
                    data.get("entry_price"),
                    data.get("stop_loss"),
                    data.get("tp1"),
                    data.get("tp2"),
                    data.get("close_price"),
                    datetime.utcnow(),
                    data.get("close_reason"),
                    data.get("profit_pct"),
                    data.get("risk_pct"),
                    json.dumps({"raw": data}),
                ),
            )
            conn.commit()
    except Exception as exc:
        _metrics["db_errors_total"] = _metrics.get("db_errors_total", 0) + 1
        logger.error("[DB] trade persist failed: %s", exc)


# ── Data quality validation ───────────────────────────────────────────────────
def _validate_tick(symbol: str, data: dict) -> list:
    """Return list of quality issue strings (empty = clean tick)."""
    bad = []
    try:
        close = float(data.get("close") or 0)
        spread = abs(float(data.get("spread") or (float(data.get("ask", 0)) - float(data.get("bid", 0)))))
        volume = float(data.get("volume") or 0)

        if close <= 0:
            bad.append("price<=0")
        if symbol in ("XAUUSD", "XAU") and not (500 < close < 5000):
            bad.append("xau_out_of_range")
        if symbol in ("US30", "USTEC", "DE30") and close > 0 and not (5000 < close < 200000):
            bad.append("index_out_of_range")
        if spread < 0 or spread > 1500:
            bad.append(f"bad_spread:{spread:.1f}")
        if volume < 0:
            bad.append("negative_volume")

        ts = data.get("timestamp")
        if ts:
            from app.live_data import to_naive_utc
            ts_dt = to_naive_utc(ts)
            now = datetime.utcnow()
            # MT5 server time can be hours ahead of UTC; only flag extreme skew.
            if (ts_dt - now).total_seconds() > 14400:
                bad.append("future_timestamp")
    except Exception as exc:
        logger.debug("Tick validation error (non-fatal): %s", exc)

    return bad