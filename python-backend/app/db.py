"""
CapriQuant db.py — Drop-in replacement.

FIX 3: DB Connection Pooling
- Global bare `conn` / `cursor` removed entirely.
- Every query uses the `db_cursor()` context manager which acquires from the pool
  and guarantees release (+ rollback on error) even under concurrent uvicorn workers.
- Pool size increased from 6 → 10 to match expected concurrency under uvicorn.
- `conn = None` stubs kept so any file that still imports `conn, cursor` gets None
  instead of a crash, but those imports should be removed over time.
- All existing functions (persist_trade, get_recent_loss_streak, etc.) are unchanged
  in their public API so the rest of the codebase needs zero edits.

FIX 2 (partial): Prometheus pool-pressure gauge updated on every acquire/release.
"""

import os
import json
import logging
from contextlib import contextmanager
from datetime import datetime, date as _date
from typing import Optional

import psycopg2
from psycopg2 import pool as _pg_pool
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pool — single global instance, initialised on first use
# ---------------------------------------------------------------------------

_db_pool: Optional[_pg_pool.ThreadedConnectionPool] = None

# We use ThreadedConnectionPool (not SimpleConnectionPool) because uvicorn
# spawns multiple threads for background tasks even in a single-worker setup.
_POOL_MIN = 2
_POOL_MAX = 10


def get_pool() -> _pg_pool.ThreadedConnectionPool:
    global _db_pool
    if _db_pool is None:
        _db_pool = _pg_pool.ThreadedConnectionPool(
            _POOL_MIN,
            _POOL_MAX,
            dbname=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
        )
        logger.info(
            "[DB] Connection pool created: min=%d max=%d host=%s db=%s",
            _POOL_MIN, _POOL_MAX,
            os.getenv("DB_HOST"), os.getenv("DB_NAME"),
        )
    return _db_pool


# ---------------------------------------------------------------------------
# Safe context manager — use this EVERYWHERE instead of get_conn_cursor()
# ---------------------------------------------------------------------------

@contextmanager
def db_cursor():
    """
    Acquire a pooled connection, yield (conn, cursor), commit on success,
    rollback + re-raise on any exception, and always return the connection
    to the pool.

    Usage:
        with db_cursor() as (conn, cur):
            cur.execute("SELECT ...")
            rows = cur.fetchall()
        # connection is back in pool here — no explicit release needed
    """
    conn = None
    cur = None
    p = get_pool()
    try:
        conn = p.getconn()
        cur = conn.cursor()
        # Update pool pressure metric if prometheus is wired
        try:
            from app.metrics import DB_POOL_USED
            used = _POOL_MAX - len(getattr(p, '_pool', []))
            DB_POOL_USED.set(max(0, used))
        except Exception:
            pass
        yield conn, cur
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        if conn and p:
            try:
                p.putconn(conn)
            except Exception:
                pass
        # Update metric on release
        try:
            from app.metrics import DB_POOL_USED
            used = _POOL_MAX - len(getattr(p, '_pool', []))
            DB_POOL_USED.set(max(0, used))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Legacy stubs — imported by old code; do NOT use for new queries.
# Remove these imports from main.py over time.
# ---------------------------------------------------------------------------

conn = None    # noqa: E305  (was global bare connection — now stub)
cursor = None  # noqa: E305  (was global bare cursor — now stub)


# ---------------------------------------------------------------------------
# Compatibility shim for callers that still use get_conn_cursor()
# ---------------------------------------------------------------------------

def get_conn_cursor():
    """
    DEPRECATED — use `with db_cursor() as (conn, cur):` instead.
    Returns a raw (conn, cur) pair from the pool. Caller MUST call
    release_conn(cur, conn) or the connection leaks.
    """
    p = get_pool()
    c = p.getconn()
    return c, c.cursor()


def release_conn(cur, conn):
    """Companion to get_conn_cursor(). Use db_cursor() context manager instead."""
    try:
        if cur:
            cur.close()
    except Exception:
        pass
    try:
        if conn and get_pool():
            get_pool().putconn(conn)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

_live_tables_ready = False


def ensure_live_tables():
    """Idempotent DDL. Creates all required tables + new execution tables for Fix 1."""
    global _live_tables_ready
    if _live_tables_ready:
        return

    ddl = [
        """
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
        """,
        "CREATE INDEX IF NOT EXISTS idx_live_signals_sym_ts ON live_signals (symbol, ts DESC);",
        """
        CREATE TABLE IF NOT EXISTS executed_trades (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ DEFAULT NOW(),
            symbol TEXT NOT NULL,
            direction TEXT,
            entry_price DOUBLE PRECISION,
            stop_loss DOUBLE PRECISION,
            tp1 DOUBLE PRECISION,
            tp2 DOUBLE PRECISION,
            r_multiple DOUBLE PRECISION,
            outcome TEXT,
            volume_lots DOUBLE PRECISION,
            signal_id BIGINT,
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            ticket BIGINT,
            close_price DOUBLE PRECISION,
            close_ts TIMESTAMPTZ,
            close_reason TEXT,
            entry_context JSONB,
            setup TEXT
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_exec_trades_sym_ts ON executed_trades (symbol, ts DESC);",
        "CREATE INDEX IF NOT EXISTS idx_exec_trades_ticket ON executed_trades (ticket);",
        """
        CREATE TABLE IF NOT EXISTS structure_events (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ DEFAULT NOW(),
            symbol TEXT NOT NULL,
            event_type TEXT,
            direction TEXT,
            price DOUBLE PRECISION,
            details JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """,
        # FIX 1 — execution feedback tables
        """
        CREATE TABLE IF NOT EXISTS execution_fills (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ DEFAULT NOW(),
            cid TEXT,
            symbol TEXT NOT NULL,
            direction TEXT,
            lot DOUBLE PRECISION,
            req_price DOUBLE PRECISION,
            fill_price DOUBLE PRECISION,
            slippage DOUBLE PRECISION,
            latency_ms INTEGER,
            deal_id BIGINT,
            ticket BIGINT,
            event_type TEXT DEFAULT 'fill',
            retcode INTEGER,
            reject_reason TEXT,
            notes TEXT
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_exec_fills_sym_ts ON execution_fills (symbol, ts DESC);",
        "CREATE INDEX IF NOT EXISTS idx_exec_fills_cid ON execution_fills (cid);",
        "CREATE INDEX IF NOT EXISTS idx_exec_fills_ticket ON execution_fills (ticket);",
        # market_data table for tick archival (used by _do_persist). Must have a
        # unique constraint so ON CONFLICT works and we avoid duplicate spam.
        """
        CREATE TABLE IF NOT EXISTS market_data (
            id BIGSERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL,
            open DOUBLE PRECISION,
            high DOUBLE PRECISION,
            low DOUBLE PRECISION,
            close DOUBLE PRECISION,
            tick_volume DOUBLE PRECISION,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_market_data_sym_ts ON market_data (symbol, timestamp DESC);",
    ]

    extra_cols = [
        "ticket BIGINT",
        "close_price DOUBLE PRECISION",
        "close_ts TIMESTAMPTZ",
        "close_reason TEXT",
        "entry_context JSONB",
        "setup TEXT",
    ]

    try:
        with db_cursor() as (c, cur):
            for stmt in ddl:
                cur.execute(stmt)
            for col in extra_cols:
                try:
                    cur.execute(
                        "ALTER TABLE executed_trades ADD COLUMN IF NOT EXISTS " + col + ";"
                    )
                except Exception:
                    pass

            # Create market_data unique constraint (best effort - safe on old PG or if dups exist)
            try:
                cur.execute("""
                    ALTER TABLE market_data
                    ADD CONSTRAINT IF NOT EXISTS uq_market_data_sym_tf_ts
                    UNIQUE (symbol, timeframe, timestamp);
                """)
            except Exception:
                pass

            c.commit()
        _live_tables_ready = True
        logger.info("[DB] Live tables ensured (including execution_fills).")
    except Exception as e:
        logger.error("[DB] ensure_live_tables failed: %s", e)


# ---------------------------------------------------------------------------
# persist_trade — unchanged public API, uses db_cursor internally
# ---------------------------------------------------------------------------

def persist_trade(trade: dict):
    """Upsert a trade by ticket. Unchanged API vs original."""
    from app.utils.symbols import normalize_symbol

    ensure_live_tables()
    sym = normalize_symbol(trade.get("symbol") or "")
    if not sym:
        return

    status = (trade.get("status") or trade.get("outcome") or "open").lower()
    outcome = trade.get("outcome") or ("closed" if status == "closed" else "open")
    ticket = trade.get("ticket")
    if ticket is not None:
        try:
            ticket = int(ticket)
        except (TypeError, ValueError):
            ticket = None

    # Auto-compute r_multiple on closes
    if status == "closed" and not trade.get("r_multiple"):
        ent = trade.get("entry_price")
        stp = trade.get("stop_loss")
        cp = trade.get("close_price")
        direc = trade.get("direction") or "BUY"
        if (not ent or not stp) and ticket:
            try:
                with db_cursor() as (c2, cur2):
                    cur2.execute(
                        "SELECT entry_price, stop_loss, direction FROM executed_trades "
                        "WHERE ticket = %s ORDER BY ts DESC LIMIT 1",
                        (ticket,),
                    )
                    row = cur2.fetchone()
                    if row:
                        if not ent:
                            ent = row[0]
                        if not stp:
                            stp = row[1]
                        if not direc or direc in ("CLOSE", "SYSTEM"):
                            direc = row[2]
            except Exception:
                pass
        if ent and stp and cp:
            rm = _compute_r_multiple(direc, ent, cp, stp)
            if rm != 0.0:
                trade["r_multiple"] = rm

    bad_setups = (None, "", "open", "open_update", "closed", "flatten", "SYSTEM", "CLOSE")
    raw_setup = trade.get("setup")
    safe_setup = None
    if (
        raw_setup
        and str(raw_setup).strip()
        and str(raw_setup).strip() not in bad_setups
        and not str(raw_setup).startswith("open")
    ):
        safe_setup = str(raw_setup).strip()

    try:
        with db_cursor() as (c, cur):
            if ticket:
                cur.execute(
                    """
                    UPDATE executed_trades SET
                        symbol       = COALESCE(%s, symbol),
                        direction    = COALESCE(%s, direction),
                        entry_price  = COALESCE(%s, entry_price),
                        stop_loss    = COALESCE(%s, stop_loss),
                        tp1          = COALESCE(%s, tp1),
                        tp2          = COALESCE(%s, tp2),
                        close_price  = COALESCE(%s, close_price),
                        close_ts     = COALESCE(%s, close_ts),
                        close_reason = COALESCE(%s, close_reason),
                        outcome      = COALESCE(%s, outcome),
                        r_multiple   = COALESCE(%s, r_multiple),
                        volume_lots  = COALESCE(%s, volume_lots),
                        notes        = COALESCE(%s, notes),
                        entry_context= COALESCE(%s, entry_context),
                        setup        = COALESCE(%s, setup)
                    WHERE ticket = %s
                    """,
                    (
                        sym,
                        trade.get("direction"),
                        trade.get("entry_price"),
                        trade.get("stop_loss"),
                        trade.get("tp1"),
                        trade.get("tp2"),
                        trade.get("close_price"),
                        trade.get("close_ts") or (datetime.utcnow() if status == "closed" else None),
                        trade.get("close_reason"),
                        outcome,
                        trade.get("r_multiple"),
                        trade.get("volume_lots"),
                        trade.get("notes"),
                        trade.get("entry_context"),
                        safe_setup,
                        ticket,
                    ),
                )
                if cur.rowcount == 0:
                    cur.execute(
                        """
                        INSERT INTO executed_trades
                            (symbol, direction, entry_price, stop_loss, tp1, tp2,
                             r_multiple, outcome, volume_lots, notes, ticket,
                             close_price, close_ts, close_reason, entry_context, setup)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            sym,
                            trade.get("direction"),
                            trade.get("entry_price"),
                            trade.get("stop_loss"),
                            trade.get("tp1"),
                            trade.get("tp2"),
                            trade.get("r_multiple"),
                            outcome,
                            trade.get("volume_lots"),
                            trade.get("notes"),
                            ticket,
                            trade.get("close_price"),
                            trade.get("close_ts"),
                            trade.get("close_reason"),
                            trade.get("entry_context"),
                            safe_setup,
                        ),
                    )
            else:
                cur.execute(
                    """
                    INSERT INTO executed_trades
                        (symbol, direction, entry_price, stop_loss, tp1, tp2,
                         r_multiple, outcome, volume_lots, notes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        sym,
                        trade.get("direction"),
                        trade.get("entry_price"),
                        trade.get("stop_loss"),
                        trade.get("tp1"),
                        trade.get("tp2"),
                        trade.get("r_multiple"),
                        outcome,
                        trade.get("volume_lots"),
                        trade.get("notes"),
                    ),
                )
            c.commit()
    except Exception as e:
        logger.warning("persist_trade warning: %s", e)


ensure_live_tables()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_r_multiple(direction: str, entry: float, close_p: float, stop: float) -> float:
    if entry is None or close_p is None or stop is None:
        return 0.0
    try:
        entry = float(entry)
        close_p = float(close_p)
        stop = float(stop)
        risk = abs(entry - stop)
        if risk < 1e-8:
            return 0.0
        reward = (close_p - entry) if str(direction).upper() == "BUY" else (entry - close_p)
        return round(reward / risk, 4)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Risk context queries
# ---------------------------------------------------------------------------

def get_recent_loss_streak(symbol: str = None, lookback: int = 12) -> int:
    try:
        params = []
        sym_clause = ""
        if symbol:
            sym_clause = "AND symbol = %s"
            params = [symbol]
        q = f"""
            SELECT r_multiple, close_reason, outcome
            FROM executed_trades
            WHERE (close_reason IS NOT NULL OR outcome NOT IN ('open','') OR r_multiple IS NOT NULL)
              {sym_clause}
            ORDER BY COALESCE(close_ts, ts) DESC
            LIMIT %s
        """
        with db_cursor() as (conn, cur):
            try:
                conn.rollback()
            except Exception:
                pass
            cur.execute(q, tuple(params + [lookback]))
            rows = cur.fetchall()
        streak = 0
        for r in rows:
            rm = r[0] if r[0] is not None else 0.0
            cr = (r[1] or "").lower()
            oc = (r[2] or "").lower()
            is_loss = (rm < 0) or ("sl" in cr) or (oc in ("loss", "sl", "stop"))
            if is_loss:
                streak += 1
            else:
                break
        return streak
    except Exception as e:
        logger.error("[DB] get_recent_loss_streak failed: %s", e)
        return 0


def get_today_realized_r(symbol: str = None) -> float:
    try:
        today = _date.today()
        params = [today]
        sym_clause = ""
        if symbol:
            sym_clause = "AND symbol = %s"
            params.append(symbol)
        q = f"""
            SELECT COALESCE(SUM(r_multiple), 0.0)
            FROM executed_trades
            WHERE DATE(COALESCE(close_ts, ts)) = %s
              AND (close_reason IS NOT NULL OR (outcome NOT IN ('open','') AND r_multiple IS NOT NULL))
              {sym_clause}
        """
        with db_cursor() as (conn, cur):
            try:
                conn.rollback()
            except Exception:
                pass
            cur.execute(q, tuple(params))
            row = cur.fetchone()
        return float(row[0] or 0.0) if row else 0.0
    except Exception as e:
        logger.error("[DB] get_today_realized_r failed: %s", e)
        return 0.0