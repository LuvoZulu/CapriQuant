"""
System Mode / Kill Switch (shared module).

Provides non-bypassable control: trading | paused | flatten.
Persisted to logs/system_mode.json for restart safety.
Used by realtime ingest, /signal, /control, management, etc.

Avoids circular imports between main <-> api.
"""
import os
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

# Module-relative logs/ for cwd independence (matches signal_logger fix)
_CONTROL_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_CONTROL_DIR.mkdir(exist_ok=True)
CONTROL_STATE_FILE = _CONTROL_DIR / "system_mode.json"

SYSTEM_MODE = "trading"  # default

# Simple recent ingest quality (last N bad per symbol) for /status
_QUALITY_BAD: Dict[str, list] = {}  # symbol -> list of (ts, reasons)
_MAX_QUALITY_BAD = 5


def _load_system_mode() -> str:
    global SYSTEM_MODE
    try:
        if CONTROL_STATE_FILE.exists():
            with open(CONTROL_STATE_FILE) as f:
                data = json.load(f)
                m = data.get("mode", "trading")
                if m in ("trading", "paused", "flatten"):
                    SYSTEM_MODE = m
    except Exception:
        pass
    return SYSTEM_MODE


def _save_system_mode(mode: str) -> str:
    global SYSTEM_MODE
    if mode not in ("trading", "paused", "flatten"):
        mode = "trading"
    SYSTEM_MODE = mode
    try:
        with open(CONTROL_STATE_FILE, "w") as f:
            json.dump({"mode": mode, "ts": datetime.utcnow().isoformat()}, f)
    except Exception as e:
        try:
            import logging
            logging.getLogger(__name__).error(f"Failed to persist system mode: {e}")
        except Exception:
            pass
    return SYSTEM_MODE


# init on import
_load_system_mode()


def get_system_mode() -> str:
    return SYSTEM_MODE


def set_system_mode(mode: str) -> str:
    return _save_system_mode(mode)


def _apply_system_mode_to_signal(signal_dict: dict, symbol: str = "") -> dict:
    """If system not in trading mode, override signal to HOLD or special FLATTEN."""
    mode = get_system_mode()
    if mode == "trading":
        return signal_dict
    out = dict(signal_dict) if isinstance(signal_dict, dict) else {"signal": "HOLD"}
    if mode == "flatten":
        out["signal"] = "FLATTEN"
        out["rationale"] = (out.get("rationale", "") + " | SYSTEM FLATTEN: close all positions immediately.").strip()
        out["action"] = "flatten_all"
    else:  # paused
        out["signal"] = "HOLD"
        out["rationale"] = (out.get("rationale", "") + " | SYSTEM PAUSED via kill switch / control.").strip()
    out["system_mode"] = mode
    try:
        import logging
        logging.getLogger(__name__).warning(f"[SYSTEM MODE] {symbol} overridden to {out['signal']} (mode={mode})")
    except Exception:
        pass
    return out


def _flatten_signal_for_ea(response: dict, signal_result: dict) -> None:
    """
    MT5 EA parses flat sig_* keys from POST /market-data responses.
    The nested 'signal' object alone is not readable by the simple MQL5 JSON helpers.
    """
    if not isinstance(signal_result, dict):
        return
    response["sig_dir"] = signal_result.get("signal")
    response["sig_confidence"] = signal_result.get("confidence")
    response["sig_setup"] = signal_result.get("setup")
    response["sig_rationale"] = signal_result.get("rationale")
    stop = signal_result.get("validated_stop") or signal_result.get("stop_suggestion")
    response["sig_stop_suggestion"] = stop
    response["sig_tp1"] = signal_result.get("tp1")
    response["sig_tp2"] = signal_result.get("tp2")
    mgmt = signal_result.get("management")
    if isinstance(mgmt, dict):
        response["management_action"] = mgmt.get("action")
        response["management_new_sl"] = mgmt.get("new_sl")
        response["management_reason"] = mgmt.get("reason")


def record_quality_bad(symbol: str, reasons: list) -> None:
    global _QUALITY_BAD
    if symbol not in _QUALITY_BAD:
        _QUALITY_BAD[symbol] = []
    _QUALITY_BAD[symbol].append((datetime.utcnow().isoformat(), reasons))
    if len(_QUALITY_BAD[symbol]) > _MAX_QUALITY_BAD:
        _QUALITY_BAD[symbol] = _QUALITY_BAD[symbol][- _MAX_QUALITY_BAD :]


def get_quality_issues() -> Dict[str, list]:
    return {s: list(bads) for s, bads in _QUALITY_BAD.items()}


def _compute_current_alerts() -> list:
    """Basic alerting based on current risk state + mode."""
    from app.db import get_recent_loss_streak, get_today_realized_r
    alerts = []
    mode = get_system_mode()
    if mode != "trading":
        alerts.append({"level": "critical", "type": "system_mode", "msg": f"System in {mode} mode - trading restricted"})

    try:
        streak = get_recent_loss_streak(None) or 0
        today_r = get_today_realized_r(None) or 0.0
        if streak >= 3:
            alerts.append({"level": "warning", "type": "streak", "msg": f"Loss streak = {streak}"})
        if today_r < -3.0:  # rough R threshold
            alerts.append({"level": "warning", "type": "daily_loss", "msg": f"Today realized R ~ {today_r:.1f}"})
    except Exception:
        pass

    # quality issues
    for s, bads in _QUALITY_BAD.items():
        if len(bads) > 2:
            alerts.append({"level": "info", "type": "data_quality", "msg": f"{s} has {len(bads)} recent bad ticks"})

    return alerts
