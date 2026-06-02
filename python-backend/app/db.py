import os
import json
from datetime import datetime
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import Json

load_dotenv()

conn = psycopg2.connect(
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    host=os.getenv("DB_HOST"),
    port=os.getenv("DB_PORT")
)

cursor = conn.cursor()


def ensure_live_tables():
    """
    Idempotent table creation for live system observability.
    Called on startup so the DB is always ready for the UI and historical queries.
    """
    # Rich signal history (every time we compute a signal, realtime or polled)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS live_signals (
        id BIGSERIAL PRIMARY KEY,
        ts TIMESTAMPTZ DEFAULT NOW(),
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        signal TEXT,
        score DOUBLE PRECISION,
        confidence DOUBLE PRECISION,
        setup TEXT,
        rationale TEXT,
        structure_summary TEXT,
        bias TEXT,
        session_phase TEXT,
        current_price DOUBLE PRECISION,
        market_structure JSONB,
        confluences JSONB,
        buffer_bars INTEGER,
        raw_response JSONB,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)

    # Trades reported by the AutoTrader EA (or simulated)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS executed_trades (
        id BIGSERIAL PRIMARY KEY,
        ts TIMESTAMPTZ DEFAULT NOW(),
        symbol TEXT NOT NULL,
        direction TEXT,               -- BUY / SELL
        entry_price DOUBLE PRECISION,
        stop_loss DOUBLE PRECISION,
        tp1 DOUBLE PRECISION,
        tp2 DOUBLE PRECISION,
        r_multiple DOUBLE PRECISION,  -- reward/risk realized or projected
        outcome TEXT,                 -- 'win', 'loss', 'open', 'breakeven'
        volume_lots DOUBLE PRECISION,
        signal_id BIGINT REFERENCES live_signals(id),
        notes TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)

    # Optional: lightweight structure events for deep historical analysis in the UI
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS structure_events (
        id BIGSERIAL PRIMARY KEY,
        ts TIMESTAMPTZ DEFAULT NOW(),
        symbol TEXT NOT NULL,
        event_type TEXT,              -- BOS, CHOCH, OB_CREATED, FVG_CREATED, etc.
        direction TEXT,
        price DOUBLE PRECISION,
        details JSONB,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)

    try:
        conn.commit()
    except Exception:
        conn.rollback()


def persist_signal(signal_dict: dict, symbol: str, timeframe: str, buffer_bars: int = 0):
    """
    Store every computed signal (realtime from TICK or from /signal polls) into live_signals.
    This powers the "progress of the building up of the signal" view + historical charts in the UI.
    """
    ensure_live_tables()
    try:
        market_structure = signal_dict.get("market_structure") or {}
        cursor.execute("""
            INSERT INTO live_signals
            (symbol, timeframe, signal, score, confidence, setup, rationale,
             structure_summary, bias, session_phase, current_price, market_structure,
             confluences, buffer_bars, raw_response)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            symbol,
            timeframe,
            signal_dict.get("signal"),
            signal_dict.get("score"),
            signal_dict.get("confidence"),
            signal_dict.get("setup"),
            signal_dict.get("rationale"),
            signal_dict.get("structure_summary"),
            market_structure.get("bias") or signal_dict.get("bias"),
            market_structure.get("session_phase") or signal_dict.get("session"),
            signal_dict.get("current_price") or market_structure.get("current_price"),
            Json(market_structure),
            Json(signal_dict.get("confluences", [])),
            buffer_bars,
            Json(signal_dict)
        ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        # Silent best-effort; do not break signal generation
        print(f"DB persist_signal warning: {e}")


def persist_trade(trade: dict):
    """
    Called by the EA (via POST /report-trade) or internally when a signal leads to execution.
    """
    ensure_live_tables()
    try:
        cursor.execute("""
            INSERT INTO executed_trades
            (symbol, direction, entry_price, stop_loss, tp1, tp2, r_multiple, outcome, volume_lots, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            trade.get("symbol"),
            trade.get("direction"),
            trade.get("entry_price"),
            trade.get("stop_loss"),
            trade.get("tp1"),
            trade.get("tp2"),
            trade.get("r_multiple"),
            trade.get("outcome", "open"),
            trade.get("volume_lots"),
            trade.get("notes"),
        ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"DB persist_trade warning: {e}")


# Ensure tables exist as soon as the module is imported (works for both API and UI processes)
ensure_live_tables()
