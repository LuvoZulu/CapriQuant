"""
Multi-timeframe structure signal combiner.

Primary: M5 (structure + bias + trade decision)
Entry timing: M1 must confirm (not drive alone)
Filter: M15 must not oppose
"""

from __future__ import annotations

from typing import Dict, Optional
import pandas as pd

from app.features.builder import compute_structure
from app.engine.confluence import (
    get_structure_signal,
    generate_structure_summary,
    apply_m5_risk_levels,
)
from app.live_data import live_buffer, resample_ohlcv, filter_df_to_catchup_window
from app.config import get_settings
from app.risk import RiskManager, RiskParams
from app.db import get_recent_loss_streak, get_today_realized_r
from app.config import get_settings

# Minimum confidence (0-100) to emit BUY/SELL after MTF alignment
_s = get_settings()
MIN_TRADE_CONFIDENCE = _s.mtf_min_confidence
MIN_M5_CONFLUENCE = _s.mtf_min_m5_confluence


def _signal_direction(sig: Dict) -> str:
    return str(sig.get("signal", "HOLD")).upper()


def _bias(sig: Dict) -> str:
    return str(sig.get("bias", "NEUTRAL")).upper()


def _total_confluence(sig: Dict) -> float:
    ctx = sig.get("contextual_scores") or {}
    return float(ctx.get("total", 0))


def combine_mtf_signals(
    sig_m1: Dict,
    sig_m5: Dict,
    sig_m15: Dict,
    symbol: str,
) -> Dict:
    """
    High-precision merge: only M5 setups become trades; M1 confirms; M15 vetoes.
    """
    d1 = _signal_direction(sig_m1)
    d5 = _signal_direction(sig_m5)
    d15 = _signal_direction(sig_m15)
    b5 = _bias(sig_m5)
    b15 = _bias(sig_m15)
    b1 = _bias(sig_m1)

    mtf_notes = [
        f"M1={d1}({b1})",
        f"M5={d5}({b5})",
        f"M15={d15}({b15})",
    ]

    # Only M5 may trigger a trade (fixes inaccurate M1-only signals)
    if d5 not in ("BUY", "SELL"):
        base = sig_m5 if sig_m5.get("structure_summary") else sig_m1
        hold = dict(base)
        hold["signal"] = "HOLD"
        hold["engine"] = "structure_mtf_precision"
        hold["setup"] = None
        hold["rationale"] = f"No M5 setup. {' | '.join(mtf_notes)}"
        hold["mtf"] = {"M1": _compact(sig_m1), "M5": _compact(sig_m5), "M15": _compact(sig_m15)}
        return hold

    direction = d5
    candidate = dict(sig_m5)

    # M5 bias must align with trade
    if direction == "BUY" and b5 == "BEARISH":
        return _hold(sig_m5, f"M5 bearish bias blocks BUY. {' | '.join(mtf_notes)}", sig_m1, sig_m5, sig_m15)
    if direction == "SELL" and b5 == "BULLISH":
        return _hold(sig_m5, f"M5 bullish bias blocks SELL. {' | '.join(mtf_notes)}", sig_m1, sig_m5, sig_m15)

    # M1 must confirm (same direction or neutral bias — never opposite)
    if d1 in ("BUY", "SELL") and d1 != direction:
        return _hold(sig_m5, f"M1 opposes M5 {direction}. {' | '.join(mtf_notes)}", sig_m1, sig_m5, sig_m15)
    if direction == "BUY" and b1 == "BEARISH":
        return _hold(sig_m5, f"M1 bearish blocks BUY confirm. {' | '.join(mtf_notes)}", sig_m1, sig_m5, sig_m15)
    if direction == "SELL" and b1 == "BULLISH":
        return _hold(sig_m5, f"M1 bullish blocks SELL confirm. {' | '.join(mtf_notes)}", sig_m1, sig_m5, sig_m15)

    # M15 hard veto
    if direction == "BUY" and (b15 == "BEARISH" or d15 == "SELL"):
        return _hold(sig_m5, f"M15 opposes BUY. {' | '.join(mtf_notes)}", sig_m1, sig_m5, sig_m15)
    if direction == "SELL" and (b15 == "BULLISH" or d15 == "BUY"):
        return _hold(sig_m5, f"M15 opposes SELL. {' | '.join(mtf_notes)}", sig_m1, sig_m5, sig_m15)

    # Confluence + confidence gates on M5
    m5_conf = float(candidate.get("confidence", 0))
    m5_total = _total_confluence(sig_m5)
    if m5_conf < MIN_TRADE_CONFIDENCE:
        return _hold(
            sig_m5,
            f"M5 confidence {m5_conf:.1f}% < {MIN_TRADE_CONFIDENCE}%. {' | '.join(mtf_notes)}",
            sig_m1, sig_m5, sig_m15,
        )
    if m5_total < MIN_M5_CONFLUENCE:
        return _hold(
            sig_m5,
            f"M5 confluence {m5_total:.2f} < {MIN_M5_CONFLUENCE}. {' | '.join(mtf_notes)}",
            sig_m1, sig_m5, sig_m15,
        )

    if not candidate.get("setup"):
        return _hold(sig_m5, f"M5 has no named setup. {' | '.join(mtf_notes)}", sig_m1, sig_m5, sig_m15)

    # Alignment bonus (capped)
    align_bonus = 0.0
    if d1 == direction:
        align_bonus += 3.0
    if d15 == direction:
        align_bonus += 2.0
    if (direction == "BUY" and b5 == "BULLISH") or (direction == "SELL" and b5 == "BEARISH"):
        align_bonus += 4.0

    result = dict(candidate)
    result["confidence"] = round(min(97.0, m5_conf + align_bonus), 1)
    result["engine"] = "structure_mtf_precision"
    result["rationale"] = f"{candidate.get('rationale', '')} | MTF precision: {' | '.join(mtf_notes)}"
    result["mtf"] = {"M1": _compact(sig_m1), "M5": _compact(sig_m5), "M15": _compact(sig_m15)}
    result["timeframe"] = "MTF(M1+M5+M15)"
    return result


def _compact(sig: Dict) -> Dict:
    return {
        "signal": sig.get("signal"),
        "bias": sig.get("bias"),
        "setup": sig.get("setup"),
        "confidence": sig.get("confidence"),
        "total_confluence": (sig.get("contextual_scores") or {}).get("total", 0),
    }


def _hold(base: Dict, reason: str, s1: Dict, s5: Dict, s15: Dict) -> Dict:
    out = dict(base)
    out["signal"] = "HOLD"
    out["score"] = 0.0
    out["confidence"] = 0.0
    out["setup"] = None
    out["engine"] = "structure_mtf_precision"
    out["rationale"] = reason
    out["mtf"] = {"M1": _compact(s1), "M5": _compact(s5), "M15": _compact(s15)}
    out["timeframe"] = "MTF(M1+M5+M15)"
    return out


def get_mtf_structure_signal(
    symbol: str,
    spread: float = 0.0,
    min_candles_m1: int = 8,
    equity: float = 0.0,
) -> Optional[Dict]:
    """
    Build M1/M5/M15 signals from the live M1 buffer and return combined output.
    Returns None if insufficient live data.
    equity: optional live equity for RiskManager hard veto (account-level circuits).
    """
    max_m1 = int(get_settings().catchup_max_m1_bars)
    df_m1_full = live_buffer.get_recent_df(symbol, limit=max_m1)
    df_m1_full = filter_df_to_catchup_window(df_m1_full)
    if df_m1_full is None or len(df_m1_full) < min_candles_m1:
        return None

    current_price = float(df_m1_full["close"].iloc[-1])

    # Prefer closed bars for structure (last 24h only — no deep rollback after downtime)
    df_m1 = live_buffer.get_recent_df_for_structure(symbol, limit=max_m1)
    if df_m1 is None or (hasattr(df_m1, 'empty') and df_m1.empty):
        df_m1 = (df_m1_full.iloc[:-1].reset_index(drop=True) if len(df_m1_full) > 1 else df_m1_full)

    max_m5 = max(3, max_m1 // 5)
    df_m5 = live_buffer.get_recent_m5_df(symbol, limit=max_m5)
    if df_m5 is None or df_m5.empty:
        df_m5 = resample_ohlcv(df_m1_full, minutes=5)
    df_m15 = resample_ohlcv(df_m1_full, minutes=15)
    df_m5 = filter_df_to_catchup_window(df_m5) if df_m5 is not None else df_m5
    df_m15 = filter_df_to_catchup_window(df_m15) if df_m15 is not None else df_m15

    # Use only completed (closed) bars for higher TF structure to avoid noise from forming candle (fixes 0 swings)
    if len(df_m5) > 1:
        df_m5 = df_m5.iloc[:-1].reset_index(drop=True)
    if len(df_m15) > 1:
        df_m15 = df_m15.iloc[:-1].reset_index(drop=True)

    ms_m5 = None
    if len(df_m5) >= 3:
        ms_m5 = compute_structure(df_m5, symbol=symbol, timeframe="M5", min_candles=30)
        ms_m5.current_price = current_price

    if len(df_m5) < 3 or len(df_m15) < 2 or ms_m5 is None:
        return {
            "signal": "HOLD",
            "score": 0.0,
            "confidence": 0.0,
            "engine": "structure_mtf_precision",
            "setup": None,
            "rationale": "Building M5/M15 history — wait for buffer backfill or more live bars.",
            "bias": ms_m5.bias if ms_m5 else "NEUTRAL",
            "current_price": current_price,
            "buffer_status": live_buffer.get_buffer_status(symbol),
        }

    ms_m1 = compute_structure(df_m1, symbol=symbol, timeframe="M1", min_candles=min_candles_m1)
    ms_m15 = compute_structure(df_m15, symbol=symbol, timeframe="M15", min_candles=20)

    for ms in (ms_m1, ms_m5, ms_m15):
        ms.current_price = current_price

    sig_m1 = get_structure_signal(ms_m1, spread=spread)
    sig_m5 = get_structure_signal(ms_m5, spread=spread)
    sig_m15 = get_structure_signal(ms_m15, spread=spread)

    combined = combine_mtf_signals(sig_m1, sig_m5, sig_m15, symbol)
    combined["current_price"] = current_price
    combined["structure_summary"] = generate_structure_summary(ms_m5)
    combined["bias"] = ms_m5.bias
    combined["market_structure"] = ms_m5.to_dict()
    combined["buffer_status"] = live_buffer.get_buffer_status(symbol)

    if combined.get("signal") in ("BUY", "SELL"):
        combined = apply_m5_risk_levels(combined, ms_m5, current_price)

    # === HARD RiskManager veto also for MTF final output (account level, non-bypassable) ===
    # This ensures even if MTF path is used directly, streak/daily circuits apply.
    try:
        if combined.get("signal") in ("BUY", "SELL"):
            eq = float(equity) if equity and equity > 1.0 else 200.0
            streak = get_recent_loss_streak(None) or 0
            today_r = get_today_realized_r(None) or 0.0
            avg_risk_money = eq * 0.015
            today_pnl = today_r * avg_risk_money
            params = RiskParams(account_equity=eq, starting_equity=200.0, target_equity=17000.0)
            rm = RiskManager(params)
            allowed, veto_reason, eff_risk = rm.can_take_trade(
                recent_loss_streak=streak, today_pnl=today_pnl, starting_equity_today=eq
            )
            combined["risk_pct"] = round(eff_risk, 2)
            combined["risk_streak"] = streak
            combined["risk_today_r"] = round(today_r, 2)
            if not allowed:
                combined["signal"] = "HOLD"
                combined["risk_veto"] = veto_reason
                old_r = combined.get("rationale", "")
                combined["rationale"] = f"Risk veto: {veto_reason} (streak={streak}). {old_r}".strip()
    except Exception as e:
        # non-fatal
        combined["risk_error"] = str(e)

    return combined
