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

def ensure_live_tables():
    """Idempotent tables, indexes, and close tracking columns."""
    if cursor is None:
        return
    try:
        try:
            conn.rollback()
        except:
            pass
    except:
        pass
    try:
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS live_signals (
            id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT NOW(), symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL, signal TEXT, score DOUBLE PRECISION, confidence DOUBLE PRECISION,
            setup TEXT, rationale TEXT, structure_summary TEXT, bias TEXT, session_phase TEXT,
            current_price DOUBLE PRECISION, market_structure JSONB, confluences JSONB,
            buffer_bars INTEGER, raw_response JSONB, created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_live_signals_sym_ts ON live_signals (symbol, ts DESC);")

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS executed_trades (
            id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT NOW(), symbol TEXT NOT NULL,
            direction TEXT, entry_price DOUBLE PRECISION, stop_loss DOUBLE PRECISION,
            tp1 DOUBLE PRECISION, tp2 DOUBLE PRECISION, r_multiple DOUBLE PRECISION,
            outcome TEXT, volume_lots DOUBLE PRECISION, signal_id BIGINT, notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            ticket BIGINT, close_price DOUBLE PRECISION, close_ts TIMESTAMPTZ, close_reason TEXT
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_exec_trades_sym_ts ON executed_trades (symbol, ts DESC);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_exec_trades_ticket ON executed_trades (ticket);")

        for col in ["ticket BIGINT", "close_price DOUBLE PRECISION", "close_ts TIMESTAMPTZ", "close_reason TEXT", "entry_context JSONB", "setup TEXT"]:
            try:
                cursor.execute("ALTER TABLE executed_trades ADD COLUMN IF NOT EXISTS " + col + ";")
            except: pass

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS structure_events (
            id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT NOW(), symbol TEXT NOT NULL,
            event_type TEXT, direction TEXT, price DOUBLE PRECISION, details JSONB, created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        conn.commit()
    except Exception:
        try: conn.rollback()
        except: pass

def persist_trade(trade: dict):
    """Support close updates by ticket or insert."""
    ensure_live_tables()
    if cursor is None:
        return
    try:
        ticket = trade.get("ticket")
        if ticket:
            cursor.execute("""
                UPDATE executed_trades SET
                    close_price = COALESCE(%s, close_price),
                    close_ts = COALESCE(%s, close_ts),
                    close_reason = COALESCE(%s, close_reason),
                    outcome = COALESCE(%s, outcome),
                    r_multiple = COALESCE(%s, r_multiple),
                    notes = COALESCE(%s, notes),
                    entry_context = COALESCE(%s, entry_context),
                    setup = COALESCE(%s, setup)
                WHERE ticket = %s
            """, (
                trade.get("close_price"),
                trade.get("close_ts") or (datetime.utcnow() if trade.get("status") == "closed" else None),
                trade.get("close_reason"),
                trade.get("outcome", "open"),
                trade.get("r_multiple"),
                trade.get("notes"),
                trade.get("entry_context"),
                trade.get("setup"),
                ticket
            ))
            if cursor.rowcount == 0:
                cursor.execute("""
                    INSERT INTO executed_trades (symbol, direction, entry_price, stop_loss, tp1, tp2,
                        r_multiple, outcome, volume_lots, notes, ticket, close_price, close_ts, close_reason, entry_context, setup)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    trade.get("symbol"), trade.get("direction"), trade.get("entry_price"), trade.get("stop_loss"),
                    trade.get("tp1"), trade.get("tp2"), trade.get("r_multiple"), trade.get("outcome", "open"),
                    trade.get("volume_lots"), trade.get("notes"), ticket, trade.get("close_price"),
                    trade.get("close_ts"), trade.get("close_reason"), trade.get("entry_context"), trade.get("setup")
                ))
        else:
            cursor.execute("""
                INSERT INTO executed_trades (symbol, direction, entry_price, stop_loss, tp1, tp2, r_multiple, outcome, volume_lots, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                trade.get("symbol"), trade.get("direction"), trade.get("entry_price"), trade.get("stop_loss"),
                trade.get("tp1"), trade.get("tp2"), trade.get("r_multiple"), trade.get("outcome", "open"),
                trade.get("volume_lots"), trade.get("notes")
            ))
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except: pass
        print("persist_trade warning:", e)

ensure_live_tables()


# =============================================================================
# Risk context queries (for hard RiskManager veto layer)
# =============================================================================
from datetime import datetime as _dt, date as _date

def get_recent_loss_streak(symbol: str = None, lookback: int = 12) -> int:
    """Count consecutive recent losses (r_multiple < 0 or close_reason=='sl' or outcome loss-like).
    Scans from most recent closed trades backward until a non-loss.
    """
    if cursor is None:
        return 0
    try:
        try:
            conn.rollback()
        except:
            pass
        params = []
        sym_clause = ""
        if symbol:
            sym_clause = "AND symbol = %s"
            params = [symbol]
        # Prefer closed trades that have outcome or close_reason
        q = f"""
            SELECT r_multiple, close_reason, outcome
            FROM executed_trades
            WHERE (close_reason IS NOT NULL OR outcome NOT IN ('open','') OR r_multiple IS NOT NULL)
              {sym_clause}
            ORDER BY COALESCE(close_ts, ts) DESC
            LIMIT %s
        """
        exec_params = params + [lookback] if params else [lookback]
        cursor.execute(q, exec_params)
        rows = cursor.fetchall()
        streak = 0
        for r in rows:
            rm = r[0] if r[0] is not None else 0.0
            cr = (r[1] or "").lower()
            oc = (r[2] or "").lower()
            is_loss = (rm < 0) or ("sl" in cr) or (oc in ("loss", "sl", "stop"))
            if is_loss:
                streak += 1
            else:
                break  # first non-loss stops the consecutive count
        return streak
    except Exception as e:
        print(f"[DB] get_recent_loss_streak failed: {e}")
        try:
            conn.rollback()
        except:
            pass
        return 0


def get_today_realized_r(symbol: str = None) -> float:
    """Sum of r_multiple for trades that closed today (for daily loss circuit proxy)."""
    if cursor is None:
        return 0.0
    try:
        try:
            conn.rollback()
        except:
            pass
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
        cursor.execute(q, params)
        row = cursor.fetchone()
        return float(row[0] or 0.0) if row else 0.0
    except Exception as e:
        print(f"[DB] get_today_realized_r failed: {e}")
        try:
            conn.rollback()
        except:
            pass
        return 0.0
