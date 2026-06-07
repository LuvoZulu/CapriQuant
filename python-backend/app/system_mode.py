"""
CapriQuant system_mode.py — Kill Switch + System Control (Production)
=====================================================================

Provides:
  - get_system_mode() / set_system_mode()  — global trading state
  - _apply_system_mode_to_signal()         — overlay on every outbound signal
  - _flatten_signal_for_ea()              — FLATTEN_ALL signal format
  - _compute_current_alerts()             — health alert generator
  - record_quality_bad() / get_quality_issues() — data quality tracking

State is persisted to data/system_mode.json so restarts respect kill switch.

CRITICAL: No circular imports.  This module imports nothing from app.*
except config (which has no upward deps).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── State file ───────────────────────────────────────────────────────────────
_MODE_FILE = Path("logs/system_mode.json")
_lock = threading.Lock()

_VALID_MODES = ("trading", "paused", "flatten")
_mode: str = "trading"
_mode_changed_at: Optional[datetime] = None

# ── Quality tracking (per-symbol circular buffer) ──────────────────────────
_quality_bad: Dict[str, List] = defaultdict(list)
_MAX_QUALITY_ENTRIES = 20

# ── On import: restore mode from disk ────────────────────────────────────────
def _load_mode() -> None:
    global _mode, _mode_changed_at
    try:
        if _MODE_FILE.exists():
            data = json.loads(_MODE_FILE.read_text())
            persisted = data.get("mode", "trading")
            if persisted in _VALID_MODES:
                _mode = persisted
                _mode_changed_at = datetime.utcnow()
                logger.info("[SystemMode] Restored mode from disk: %s", _mode)
    except Exception as exc:
        logger.warning("[SystemMode] Could not load persisted mode: %s", exc)


_load_mode()


# ── Public API ────────────────────────────────────────────────────────────────

def get_system_mode() -> str:
    """Return current trading mode: 'trading' | 'paused' | 'flatten'."""
    return _mode


def set_system_mode(new_mode: str) -> None:
    """
    Set trading mode.  Persisted to disk immediately.
    'flatten' auto-transitions to 'paused' after one tick cycle
    (the EA acts on FLATTEN_ALL, then we stay paused until manual resume).
    """
    global _mode, _mode_changed_at
    if new_mode not in _VALID_MODES:
        raise ValueError(f"Invalid mode '{new_mode}', must be one of {_VALID_MODES}")
    with _lock:
        _mode = new_mode
        _mode_changed_at = datetime.utcnow()
    _persist_mode()
    logger.warning("[SystemMode] Mode changed to: %s", new_mode)


def _persist_mode() -> None:
    try:
        _MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MODE_FILE.write_text(json.dumps({
            "mode": _mode,
            "changed_at": _mode_changed_at.isoformat() if _mode_changed_at else None,
        }, indent=2))
    except Exception as exc:
        logger.warning("[SystemMode] Failed to persist mode: %s", exc)


def _apply_system_mode_to_signal(signal: Dict) -> Dict:
    """
    Overlay system mode onto a generated signal.
    - paused:  always HOLD
    - flatten: FLATTEN_ALL (handled by EA as close-all + pause)
    - trading: pass through
    """
    mode = get_system_mode()
    if mode == "paused":
        return {**signal, "signal": "HOLD", "hold_reason": "system_paused"}
    if mode == "flatten":
        result = _flatten_signal_for_ea(signal)
        # Auto-transition to paused so next tick doesn't re-flatten
        set_system_mode("paused")
        return result
    return signal


def _flatten_signal_for_ea(signal: Dict) -> Dict:
    """Format a FLATTEN_ALL signal for the EA to close all positions."""
    return {
        **signal,
        "signal": "FLATTEN_ALL",
        "hold_reason": "kill_switch_flatten",
        "flatten_all": True,
        "close_reason": "kill_switch",
    }


def _compute_current_alerts() -> List[Dict]:
    """
    Generate operational alert dicts.  Called by /api/system-status.
    Returns empty list if everything is healthy.
    """
    alerts: List[Dict] = []

    # Mode alert
    mode = get_system_mode()
    if mode != "trading":
        alerts.append({
            "level": "WARNING",
            "code": f"system_{mode}",
            "message": f"System is in {mode.upper()} mode — no trades will be placed.",
            "since": _mode_changed_at.isoformat() if _mode_changed_at else None,
        })

    # Quality alert (any symbol with recent bad ticks)
    for sym, issues in list(_quality_bad.items()):
        if issues:
            recent = issues[-1]
            alerts.append({
                "level": "WARNING",
                "code": "data_quality",
                "symbol": sym,
                "message": f"Data quality issues for {sym}: {recent.get('reasons', [])}",
                "last_bad": recent.get("ts"),
            })

    # RiskManager halt alert
    try:
        from app.risk.risk_manager import get_risk_manager
        rm = get_risk_manager()
        rs = rm.get_state_dict()
        if rs.get("is_halted"):
            alerts.append({
                "level": "CRITICAL",
                "code": "risk_halt",
                "message": f"RiskManager HALTED: {rs.get('halt_reason')}",
            })
        if rs.get("loss_streak", 0) >= 3:
            alerts.append({
                "level": "WARNING",
                "code": "loss_streak",
                "message": f"Loss streak: {rs['loss_streak']} consecutive losses",
            })
    except Exception:
        pass

    return alerts


def record_quality_bad(symbol: str, reasons: List[str]) -> None:
    """Record a bad-tick event for a symbol (used by data quality gate in main.py)."""
    with _lock:
        entry = {"ts": datetime.utcnow().isoformat(), "reasons": reasons}
        _quality_bad[symbol].append(entry)
        if len(_quality_bad[symbol]) > _MAX_QUALITY_ENTRIES:
            _quality_bad[symbol] = _quality_bad[symbol][-_MAX_QUALITY_ENTRIES:]


def get_quality_issues() -> Dict[str, List]:
    """Return recent quality issues per symbol for status endpoint."""
    with _lock:
        return {k: list(v[-5:]) for k, v in _quality_bad.items() if v}