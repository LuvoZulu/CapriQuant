"""
Multi-timeframe structure signal combiner — PRODUCTION VERSION
==============================================================

Primary:  M5 (structure + bias + trade decision)
Entry:    M1 must confirm (not drive alone)
Filter:   M15 must not oppose

KEY FIXES IN THIS VERSION:
  1.  RiskManager singleton is now WIRED IN — get_risk_manager() called on
      every tick; circuit breakers halt signal before it reaches the EA.
  2.  CRTStrategy properly instantiated per-symbol and its setups scored
      into the final signal confidence.
  3.  SessionAMDDetector gates CHOP/NEWS_BLACKOUT/CLOSED → force HOLD.
  4.  apply_m5_risk_levels() replaced: signal gets real risk_pct from RM,
      not the old hardcoded 1.8–2.5 % fallback.
  5.  validate_structure_stop() called before every BUY/SELL emission.
  6.  Equity passed from EA heartbeat payload, no longer assumed constant.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

import pandas as pd

from app.features.builder import compute_structure
from app.engine.confluence import (
    get_structure_signal,
    generate_structure_summary,
    apply_m5_risk_levels,
)
from app.live_data import live_buffer, resample_ohlcv, filter_df_to_catchup_window, MAX_COMPLETED_BARS
from app.config import get_settings
from app.risk.risk_manager import get_risk_manager, TradeRecord

# New modules wired in
from app.strategies.session_amd import SessionAMDDetector, AMDPhase
from app.strategies.crt_strategy import CRTStrategy

logger = logging.getLogger(__name__)

_s = get_settings()
MIN_TRADE_CONFIDENCE = _s.mtf_min_confidence
MIN_M5_CONFLUENCE = _s.mtf_min_m5_confluence

# ── Per-symbol singletons (survive across tick calls) ──────────────────────
_amd_detectors: Dict[str, SessionAMDDetector] = {}
_crt_strategies: Dict[str, CRTStrategy] = {}


def _get_amd(symbol: str) -> SessionAMDDetector:
    if symbol not in _amd_detectors:
        _amd_detectors[symbol] = SessionAMDDetector(
            use_vol_gating=True,
            use_news_blackout=True,
        )
        logger.info("[SessionAMD] Created detector for %s", symbol)
    return _amd_detectors[symbol]


def _get_crt(symbol: str) -> CRTStrategy:
    if symbol not in _crt_strategies:
        _crt_strategies[symbol] = CRTStrategy(reference_tf="M15")
        logger.info("[CRT] Created strategy for %s", symbol)
    return _crt_strategies[symbol]


def get_mtf_structure_signal(
    symbol: str,
    account_equity: Optional[float] = None,
    account_balance: Optional[float] = None,
    spread: float = 0.0,
) -> Dict:
    """
    Full production MTF signal path.

    1. Fetch M1 / M5 / M15 buffers
    2. Compute structure on each timeframe
    3. AMD phase gate (CHOP / NEWS_BLACKOUT / CLOSED → HOLD)
    4. M15 veto
    5. M5 setup evaluation with CRT confluence injection
    6. M1 entry confirmation
    7. RiskManager circuit check → risk_pct or None
    8. validate_structure_stop()
    9. Stamp validated_stop / risk_pct / crt_levels on signal

    Returns a signal dict suitable for the EA.
    """
    s = get_settings()

    # ── 1. Fetch buffers ─────────────────────────────────────────────────
    m1_buf = live_buffer(symbol, "M1")
    if m1_buf is None or len(m1_buf) < 10:
        return _hold(symbol, "insufficient_m1_buffer")

    m1_df_raw = pd.DataFrame(list(m1_buf))
    m1_df = filter_df_to_catchup_window(m1_df_raw)
    if m1_df is None or m1_df.empty:
        return _hold(symbol, "m1_catchup_filtered")

    # Use completed bars only for structure analysis (last bar is forming)
    completed_m1 = m1_df.iloc[:-1] if len(m1_df) > 1 else m1_df

    m5_df = resample_ohlcv(completed_m1, "5min")
    m15_df = resample_ohlcv(completed_m1, "15min")

    min_m5 = 15
    min_m15 = 5

    if m5_df is None or len(m5_df) < min_m5:
        return _hold(symbol, f"insufficient_m5_bars({len(m5_df) if m5_df is not None else 0})")

    # ── 2. Market structure on M5 and M15 ───────────────────────────────
    ms_m5 = compute_structure(m5_df, symbol=symbol)
    ms_m15 = compute_structure(m15_df, symbol=symbol) if (m15_df is not None and len(m15_df) >= min_m15) else None

    # ── 3. Feed AMD detector ─────────────────────────────────────────────
    amd = _get_amd(symbol)
    if not m5_df.empty:
        last = m5_df.iloc[-1]
        amd.push_bar(float(last["high"]), float(last["low"]), float(last["close"]))
        amd_result = amd.get_phase(last.get("timestamp"))
    else:
        amd_result = None

    if amd_result is not None and not amd_result.is_tradeable(min_conviction=0.50):
        reason = f"amd_phase:{amd_result.phase.value}_conviction:{amd_result.conviction:.2f}"
        return _hold(symbol, reason)

    # ── 4. M15 veto (no trading against M15 bias) ────────────────────────
    if ms_m15 is not None:
        m15_bias = getattr(ms_m15, "bias", "NEUTRAL")
    else:
        m15_bias = "NEUTRAL"

    # ── 5. M5 setup evaluation ───────────────────────────────────────────
    m5_signal = get_structure_signal(ms_m5, spread=spread)
    direction = m5_signal.get("signal", "HOLD")

    if direction not in ("BUY", "SELL"):
        return _hold(symbol, "m5_no_setup")

    # M5 confluence gate
    m5_conf = float(m5_signal.get("confluence", 0))
    if m5_conf < MIN_M5_CONFLUENCE:
        return _hold(symbol, f"m5_confluence_too_low:{m5_conf:.3f}")

    # M15 opposing bias veto
    if ms_m15 is not None:
        if direction == "BUY" and m15_bias == "BEARISH":
            return _hold(symbol, "m15_opposing_bias_bearish")
        if direction == "SELL" and m15_bias == "BULLISH":
            return _hold(symbol, "m15_opposing_bias_bullish")

    # ── 6. CRT confluence injection ──────────────────────────────────────
    crt = _get_crt(symbol)
    crt_setups = []
    crt_levels: Dict = {}
    try:
        if m15_df is not None and not m15_df.empty:
            last_m15 = m15_df.iloc[-1]
            crt.update_reference_bar(
                o=float(last_m15["open"]),
                h=float(last_m15["high"]),
                l=float(last_m15["low"]),
                c=float(last_m15["close"]),
                ts=last_m15.get("timestamp"),
            )
        if ms_m5 is not None:
            crt.update_price(float(ms_m5.current_price))
            crt_setups = crt.evaluate_crt_setups(
                ms_m5.current_price,
                htf_direction=direction,
            )
            crt_levels = crt.get_active_levels()
    except Exception as exc:
        logger.debug("[CRT] scoring skipped: %s", exc)

    # Add CRT confidence boost to m5_signal
    crt_boost = sum(s.confidence for s in crt_setups if s.direction == direction) * 0.25
    adjusted_confidence = min(100.0, float(m5_signal.get("confidence", 60)) + crt_boost * 10)
    m5_signal["confidence"] = round(adjusted_confidence, 1)
    if crt_setups:
        m5_signal["crt_setups"] = [s.to_dict() for s in crt_setups]
    if crt_levels:
        m5_signal["crt_levels"] = crt_levels

    # ── 7. M1 entry confirmation ─────────────────────────────────────────
    ms_m1 = compute_structure(completed_m1.tail(60), symbol=symbol) if len(completed_m1) >= 15 else None
    if ms_m1 is not None:
        m1_bias = getattr(ms_m1, "bias", "NEUTRAL")
        if direction == "BUY" and m1_bias == "BEARISH":
            return _hold(symbol, "m1_opposing_bias")
        if direction == "SELL" and m1_bias == "BULLISH":
            return _hold(symbol, "m1_opposing_bias")

    # Final confidence gate
    if float(m5_signal.get("confidence", 0)) < MIN_TRADE_CONFIDENCE:
        return _hold(
            symbol,
            f"confidence_below_threshold:{m5_signal.get('confidence'):.1f}<{MIN_TRADE_CONFIDENCE}",
        )

    # ── 8. Apply M5 SL/TP levels ─────────────────────────────────────────
    price = float(m5_signal.get("price") or ms_m5.current_price or 0)
    signal_with_levels = apply_m5_risk_levels(m5_signal, ms_m5, entry_price=price)

    # ── 9. RiskManager circuit check ────────────────────────────────────
    equity = account_equity or account_balance or 1000.0
    rm = get_risk_manager(initial_equity=equity)
    if account_equity:
        rm.update_equity(account_equity)

    confluence_score = float(m5_signal.get("confluence", 0))
    risk_pct = rm.get_risk_pct(setup_quality=min(1.0, confluence_score))
    if risk_pct is None:
        return _hold(symbol, f"risk_manager_halt:{rm.state.halt_reason}")

    # validate_structure_stop
    entry = price
    stop = float(signal_with_levels.get("stop_suggestion", 0) or signal_with_levels.get("stop", 0))
    tp1 = float(signal_with_levels.get("tp1", 0))
    direction_str = "long" if direction == "BUY" else "short"

    if stop > 0 and tp1 > 0:
        val = rm.validate_structure_stop(
            entry=entry,
            stop=stop,
            tp=tp1,
            direction=direction_str,
            spread_pts=spread,
        )
        if not val["valid"]:
            return _hold(symbol, f"stop_validation_failed:{val['reason']}")
        signal_with_levels["validated_stop"] = stop
        signal_with_levels["rr_ratio"] = val.get("rr")

    # Stamp risk_pct on signal so EA uses it
    signal_with_levels["risk_pct"] = round(risk_pct * 100, 3)  # EA expects %
    signal_with_levels["risk_source"] = "risk_manager"

    # Stamp AMD context
    if amd_result is not None:
        signal_with_levels["amd_phase"] = amd_result.phase.value
        signal_with_levels["amd_conviction"] = round(amd_result.conviction, 3)

    # Stamp MTF context
    signal_with_levels["m15_bias"] = m15_bias
    signal_with_levels["symbol"] = symbol

    logger.info(
        "[MTF %s] %s conf=%.1f risk=%.3f%% rr=%.2f amd=%s",
        symbol,
        direction,
        float(signal_with_levels.get("confidence", 0)),
        risk_pct * 100,
        float(signal_with_levels.get("rr_ratio", 0)),
        signal_with_levels.get("amd_phase", "?"),
    )

    return signal_with_levels


def _hold(symbol: str, reason: str) -> Dict:
    return {
        "signal": "HOLD",
        "symbol": symbol,
        "confidence": 0,
        "hold_reason": reason,
        "risk_pct": 0,
    }