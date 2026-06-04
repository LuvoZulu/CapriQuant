"""
Simple signal + outcome logger for CapriQuant.

Logs every structure signal to:
- logs/signals.jsonl (local audit trail)
- PostgreSQL live_signals table (dashboard /api/recent-signals)
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from app.utils.symbols import normalize_symbol

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
SIGNAL_LOG = LOG_DIR / "signals.jsonl"


def _extract_symbol_timeframe(signal: Dict, symbol: Optional[str], timeframe: Optional[str]):
    sym = symbol or signal.get("symbol")
    if not sym and isinstance(signal.get("market_structure"), dict):
        sym = signal["market_structure"].get("symbol")
    tf = timeframe or signal.get("timeframe") or "M5"
    return normalize_symbol(sym) if sym else None, str(tf).upper()


def persist_signal_to_db(
    signal: Dict,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> None:
    """Insert one row into live_signals for the Streamlit dashboard."""
    sym, tf = _extract_symbol_timeframe(signal, symbol, timeframe)
    if not sym:
        return

    try:
        from app.db import db_cursor, ensure_live_tables

        ensure_live_tables()
        ms = signal.get("market_structure") if isinstance(signal.get("market_structure"), dict) else {}
        ctx = signal.get("contextual_scores") or {}
        buf = signal.get("buffer_status") or {}
        buffer_bars = buf.get("bars_in_buffer") or buf.get("effective_bars")

        with db_cursor() as (conn, cur):
            cur.execute(
                """
                INSERT INTO live_signals (
                    symbol, timeframe, signal, score, confidence, setup, rationale,
                    structure_summary, bias, session_phase, current_price,
                    market_structure, confluences, buffer_bars, raw_response
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    sym,
                    tf,
                    signal.get("signal"),
                    signal.get("score"),
                    signal.get("confidence"),
                    signal.get("setup"),
                    (signal.get("rationale") or "")[:2000],
                    signal.get("structure_summary"),
                    signal.get("bias") or ms.get("bias"),
                    signal.get("session") or ms.get("session", {}).get("phase") if isinstance(ms.get("session"), dict) else signal.get("session"),
                    signal.get("current_price") or ms.get("current_price"),
                    json.dumps(ms) if ms else None,
                    json.dumps(signal.get("confluences") or []),
                    buffer_bars,
                    json.dumps(signal, default=str),
                ),
            )
            conn.commit()
    except Exception as e:
        print(f"[signal_logger] DB persist failed for {sym}: {e}")


def log_signal(
    signal: Dict,
    outcome: Optional[Dict] = None,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> None:
    """Append a signal to JSONL and persist actionable rows to PostgreSQL."""
    record = {
        "timestamp": datetime.utcnow().isoformat(),
        "signal": signal,
        "outcome": outcome,
    }
    try:
        with open(SIGNAL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"[signal_logger] JSONL write failed: {e}")

    # Dashboard + history: log BUY/SELL always; sample HOLD when building or on named setup
    sig_dir = str(signal.get("signal", "HOLD")).upper()
    rationale = str(signal.get("rationale", ""))
    should_persist = (
        sig_dir in ("BUY", "SELL")
        or signal.get("setup")
        or "Building" in rationale
        or "Insufficient" in rationale
    )
    if should_persist:
        try:
            persist_signal_to_db(signal, symbol=symbol, timeframe=timeframe)
        except Exception as e:
            print(f"[signal_logger] persist_signal_to_db error: {e}")


def load_signals() -> list:
    """Load all logged signals from JSONL for analysis."""
    if not SIGNAL_LOG.exists():
        return []
    with open(SIGNAL_LOG, encoding="utf-8") as f:
        return [json.loads(line) for line in f]