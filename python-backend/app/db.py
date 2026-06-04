import os
import json
from datetime import datetime
from dotenv import load_dotenv
import psycopg2
from psycopg2 import pool
from psycopg2.extras import Json

load_dotenv()

# Pooled connections (MED5)
_db_pool = None

def get_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = psycopg2.pool.SimpleConnectionPool(
            1, 6,
            dbname=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT")
        )
    return _db_pool

def get_conn_cursor():
    p = get_pool()
    c = p.getconn()
    return c, c.cursor()

def release_conn(cur, conn):
    try:
        if cur: cur.close()
    except: pass
    try:
        if conn and get_pool(): get_pool().putconn(conn)
    except: pass

from contextlib import contextmanager

@contextmanager
def db_cursor():
    """Safe context manager for pooled connections. Auto release + rollback on error.
    Usage:
        with db_cursor() as (conn, cur):
            cur.execute(...)
    """
    conn = None
    cur = None
    try:
        conn, cur = get_conn_cursor()
        yield conn, cur
    except Exception:
        if conn:
            try: conn.rollback()
            except: pass
        raise
    finally:
        release_conn(cur, conn)

# Keep legacy for minimal breakage
try:
    conn = psycopg2.connect(
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT")
    )
    cursor = conn.cursor()
except Exception as _e:
    conn = None
    cursor = None
    print("[DB] Legacy conn warning, pool will be used:", _e)


# --- Appended by fix script: pooling notes + trade close schema + upsert logic ---

_live_tables_ready = False


def ensure_live_tables():
    """Idempotent tables, indexes, and close tracking columns (pooled)."""
    global _live_tables_ready
    if _live_tables_ready:
        return
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS live_signals (
            id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT NOW(), symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL, signal TEXT, score DOUBLE PRECISION, confidence DOUBLE PRECISION,
            setup TEXT, rationale TEXT, structure_summary TEXT, bias TEXT, session_phase TEXT,
            current_price DOUBLE PRECISION, market_structure JSONB, confluences JSONB,
            buffer_bars INTEGER, raw_response JSONB, created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_live_signals_sym_ts ON live_signals (symbol, ts DESC);",
        """
        CREATE TABLE IF NOT EXISTS executed_trades (
            id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT NOW(), symbol TEXT NOT NULL,
            direction TEXT, entry_price DOUBLE PRECISION, stop_loss DOUBLE PRECISION,
            tp1 DOUBLE PRECISION, tp2 DOUBLE PRECISION, r_multiple DOUBLE PRECISION,
            outcome TEXT, volume_lots DOUBLE PRECISION, signal_id BIGINT, notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            ticket BIGINT, close_price DOUBLE PRECISION, close_ts TIMESTAMPTZ, close_reason TEXT
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_exec_trades_sym_ts ON executed_trades (symbol, ts DESC);",
        "CREATE INDEX IF NOT EXISTS idx_exec_trades_ticket ON executed_trades (ticket);",
        """
        CREATE TABLE IF NOT EXISTS structure_events (
            id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT NOW(), symbol TEXT NOT NULL,
            event_type TEXT, direction TEXT, price DOUBLE PRECISION, details JSONB, created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """,
    ]
    extra_cols = [
        "ticket BIGINT", "close_price DOUBLE PRECISION", "close_ts TIMESTAMPTZ",
        "close_reason TEXT", "entry_context JSONB", "setup TEXT",
    ]
    try:
        with db_cursor() as (c, cur):
            for stmt in ddl:
                cur.execute(stmt)
            for col in extra_cols:
                try:
                    cur.execute("ALTER TABLE executed_trades ADD COLUMN IF NOT EXISTS " + col + ";")
                except Exception:
                    pass
            c.commit()
        _live_tables_ready = True
    except Exception as e:
        print(f"[DB] ensure_live_tables failed: {e}")


def persist_trade(trade: dict):
    """Support close updates by ticket or insert (pooled — works when legacy conn is down)."""
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

    # Auto-compute r_multiple on closes for risk circuits (streak/daily_r) if not provided by EA.
    # Uses prior entry/stop from the open record matched by ticket (fixes live R always 0).
    if status == "closed" and not trade.get("r_multiple"):
        ent = trade.get("entry_price")
        stp = trade.get("stop_loss")
        cp = trade.get("close_price")
        direc = trade.get("direction") or "BUY"
        if (not ent or not stp) and ticket:
            # lookup the recorded open entry/stop for this ticket
            try:
                with db_cursor() as (c2, cur2):
                    cur2.execute("SELECT entry_price, stop_loss, direction FROM executed_trades WHERE ticket = %s ORDER BY ts DESC LIMIT 1", (ticket,))
                    row = cur2.fetchone()
                    if row:
                        if not ent: ent = row[0]
                        if not stp: stp = row[1]
                        if not direc or direc in ("CLOSE", "SYSTEM"): direc = row[2]
            except Exception:
                pass
        if ent and stp and cp:
            rm = _compute_r_multiple(direc, ent, cp, stp)
            if rm != 0.0:
                trade["r_multiple"] = rm

    # Compute safe_setup: only real strategy names (OB_*, LIQUIDITY_*, TREND_*, FIB_*, CRT_* etc); never status labels
    raw_setup = trade.get("setup")
    bad_setups = (None, "", "open", "open_update", "closed", "flatten", "SYSTEM", "CLOSE")
    safe_setup = None
    if raw_setup and str(raw_setup).strip() and str(raw_setup).strip() not in bad_setups and not str(raw_setup).startswith("open"):
        safe_setup = str(raw_setup).strip()

    try:
        with db_cursor() as (c, cur):
            if ticket:
                cur.execute(
                    """
                    UPDATE executed_trades SET
                        symbol = COALESCE(%s, symbol),
                        direction = COALESCE(%s, direction),
                        entry_price = COALESCE(%s, entry_price),
                        stop_loss = COALESCE(%s, stop_loss),
                        tp1 = COALESCE(%s, tp1),
                        tp2 = COALESCE(%s, tp2),
                        close_price = COALESCE(%s, close_price),
                        close_ts = COALESCE(%s, close_ts),
                        close_reason = COALESCE(%s, close_reason),
                        outcome = COALESCE(%s, outcome),
                        r_multiple = COALESCE(%s, r_multiple),
                        volume_lots = COALESCE(%s, volume_lots),
                        notes = COALESCE(%s, notes),
                        entry_context = COALESCE(%s, entry_context),
                        setup = COALESCE(%s, setup)
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
                        INSERT INTO executed_trades (symbol, direction, entry_price, stop_loss, tp1, tp2,
                            r_multiple, outcome, volume_lots, notes, ticket, close_price, close_ts,
                            close_reason, entry_context, setup)
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
                    INSERT INTO executed_trades (symbol, direction, entry_price, stop_loss, tp1, tp2,
                        r_multiple, outcome, volume_lots, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        print(f"persist_trade warning: {e}")


ensure_live_tables()


def _compute_r_multiple(direction: str, entry: float, close_p: float, stop: float) -> float:
    """Standard R-multiple: reward/risk using structure stop as risk."""
    if entry is None or close_p is None or stop is None:
        return 0.0
    try:
        entry = float(entry)
        close_p = float(close_p)
        stop = float(stop)
        risk = abs(entry - stop)
        if risk < 1e-8:
            return 0.0
        if str(direction).upper() == "BUY":
            reward = close_p - entry
        else:
            reward = entry - close_p
        return round(reward / risk, 4)
    except Exception:
        return 0.0


# =============================================================================
# Risk context queries (for hard RiskManager veto layer)
# =============================================================================
from datetime import datetime as _dt, date as _date

def get_recent_loss_streak(symbol: str = None, lookback: int = 12) -> int:
    """Count consecutive recent losses (r_multiple < 0 or close_reason=='sl' or outcome loss-like).
    Scans from most recent closed trades backward until a non-loss.
    """
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
        exec_params = tuple(params + [lookback])
        with db_cursor() as (conn, cur):
            try:
                conn.rollback()
            except Exception:
                pass
            cur.execute(q, exec_params)
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
        print(f"[DB] get_recent_loss_streak failed: {e}")
        return 0


def get_today_realized_r(symbol: str = None) -> float:
    """Sum of r_multiple for trades that closed today (for daily loss circuit proxy)."""
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
        print(f"[DB] get_today_realized_r failed: {e}")
        return 0.0
