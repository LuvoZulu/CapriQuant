"""
CapriQuant Live Dashboard (Streamlit)

Launch with:
    streamlit run python-backend/ui/dashboard.py

Features:
- System status + live buffer fill (10080 M1 target)
- Per-symbol cards with current bias, structure_summary, active OBs, swing count
- Signal history table + simple progress charts (score over time per symbol)
- Trades section (from executed_trades reported by the EA)
- Recent structure summaries (the "progress of the building up of the signal")
- Polls the backend every few seconds for fresh data
- Works whether you open the UI once a day or leave the tab open all session

The backend (FastAPI) should be running as the Windows service.
This UI is a read-only observer you launch when you want to look.
"""

import time
import requests
import pandas as pd
import streamlit as st
from datetime import datetime

# =============================================================================
# CONFIG
# =============================================================================
BACKEND = "http://127.0.0.1:8001"   # The FastAPI backend (service) - this is where the UI fetches data from. Backend runs on 8001.
POLL_SECONDS = 5
SYMBOLS = ["US30", "USTEC", "DE30", "XAUUSD"]  # extend as you add more

# Run the UI (Streamlit) with:
#   python -m streamlit run ui/dashboard.py --server.address 127.0.0.1
# Or double-click: python-backend\ui\run_ui.bat
# (Streamlit UI itself can run on any port - default is 8501. It fetches data from the backend on 8001)

st.set_page_config(page_title="CapriQuant - Live Structure Dashboard", layout="wide")
st.title("CapriQuant • Real-time Structure & Signal Progress")

# =============================================================================
# KILL SWITCH / SYSTEM MODE CONTROLS (Phase 2 P1 - always visible, high priority)
# =============================================================================
mode_resp = {}
try:
    mode_resp = requests.get(f"{BACKEND}/api/system-mode", timeout=2).json()
except Exception:
    mode_resp = {"mode": "unknown"}
current_mode = mode_resp.get("mode", "trading")

mode_color = {"trading": "🟢", "paused": "🟡", "flatten": "🔴"}.get(current_mode, "⚪")
st.markdown(f"### System Mode: {mode_color} **{current_mode.upper()}**")

col1, col2, col3, col4 = st.columns(4)
if col1.button("🚨 EMERGENCY FLATTEN ALL + PAUSE", type="primary", use_container_width=True):
    try:
        r = requests.post(f"{BACKEND}/api/control", json={"action": "flatten_all"}, timeout=5)
        st.error(f"FLATTEN sent: {r.json()}")
        st.rerun()
    except Exception as e:
        st.error(f"Control failed: {e}")

if col2.button("⏸️ PAUSE TRADING (HOLD only)", use_container_width=True):
    try:
        r = requests.post(f"{BACKEND}/api/control", json={"action": "pause"}, timeout=5)
        st.warning(f"PAUSE sent: {r.json()}")
        st.rerun()
    except Exception as e:
        st.error(f"Control failed: {e}")

if col3.button("▶️ RESUME NORMAL TRADING", use_container_width=True):
    try:
        r = requests.post(f"{BACKEND}/api/control", json={"action": "resume"}, timeout=5)
        st.success(f"RESUME sent: {r.json()}")
        st.rerun()
    except Exception as e:
        st.error(f"Control failed: {e}")

if col4.button("Refresh mode", use_container_width=True):
    st.rerun()

if current_mode != "trading":
    st.warning(f"**SYSTEM IS IN {current_mode.upper()} MODE** — normal signals are suppressed. Use RESUME to restore trading.")

# Sidebar controls
st.sidebar.header("Controls")
auto_refresh = st.sidebar.checkbox("Auto-refresh", value=True)
refresh_sec = st.sidebar.slider("Refresh interval (s)", 3, 30, POLL_SECONDS)

if st.sidebar.button("Force refresh now"):
    st.rerun()

# =============================================================================
# HELPERS
# =============================================================================

@st.cache_data(ttl=refresh_sec)
def fetch_json(path: str, params=None):
    try:
        r = requests.get(f"{BACKEND}{path}", params=params or {}, timeout=4)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def fetch_recent_signals(symbol=None, limit=80):
    data = fetch_json("/api/recent-signals", {"symbol": symbol, "limit": limit})
    if isinstance(data, dict) and "error" in data:
        return pd.DataFrame()
    return pd.DataFrame(data)

def fetch_trades(symbol=None, limit=150):
    data = fetch_json("/api/trades", {"symbol": symbol, "limit": limit})
    if isinstance(data, dict) and "error" in data:
        return pd.DataFrame()
    return pd.DataFrame(data)

def fetch_current(symbol: str):
    return fetch_json(f"/api/current-structure/{symbol}")

def fetch_status():
    return fetch_json("/api/system-status")

def fetch_buffer():
    return fetch_json("/debug/live-buffer")

def fetch_open_trades(symbol=None, limit=50):
    """New helper for running/live open trades from the EA."""
    data = fetch_json("/api/open-trades", {"symbol": symbol, "limit": limit})
    if isinstance(data, dict) and "error" in data:
        return pd.DataFrame()
    return pd.DataFrame(data)

# =============================================================================
# TOP STATUS BAR
# =============================================================================
status = fetch_status()
col1, col2, col3, col4 = st.columns(4)
col1.metric("Backend", status.get("status", "down"))
col2.metric("Max M1 Buffer", status.get("buffer_max_m1", 10080))
tracked = status.get("symbols_tracked", [])
col3.metric("Symbols tracked", len(tracked))
col4.metric("Last poll", datetime.utcnow().strftime("%H:%M:%S"))

st.caption(f"Backend: {BACKEND}  •  Tracked: {', '.join(tracked) if tracked else 'none yet'}")

# =============================================================================
# LIVE SYMBOL CARDS + CURRENT STRUCTURE
# =============================================================================
st.subheader("Live Market Structure (from rolling 10080-bar buffer)")

card_cols = st.columns(len(SYMBOLS))
for i, sym in enumerate(SYMBOLS):
    snap = fetch_current(sym)
    with card_cols[i]:
        st.markdown(f"### {sym}")
        buf = snap.get("buffer", {}) if isinstance(snap, dict) else {}
        m5c = buf.get("m5_bars_in_buffer", 0)
        m5max = buf.get("max_m5_bars", 2016)
        m5_pct = buf.get("m5_pct_full", 0)

        # Readiness indicator based on M5 bars (primary for structure/trends)
        if m5c < 3:
            readiness = "🔴 Insufficient"
            readiness_desc = "Need ≥3 M5 bars (~15 M1) for basic structure"
        elif m5c < 8:
            readiness = "🟡 Basic"
            readiness_desc = "Weak trends (3-7 M5). Swings may be minimal."
        elif m5c < 20:
            readiness = "🟢 Good"
            readiness_desc = "Usable trends (8-19 M5). BOS/OBs starting to appear."
        else:
            readiness = "✅ Strong"
            readiness_desc = "Full context (20+ M5). Reliable structure & confluence."

        if "error" in snap or snap.get("status") == "insufficient_live_data":
            st.warning(snap.get("status", "No live data yet"))
            bcount = buf.get('effective_bars', buf.get('bars_in_buffer', 0))
            st.caption(f"Buffer: {bcount}/{buf.get('max_bars', 10080)} (incl. current minute)")
            st.caption(f"**Readiness:** {readiness}")
            st.caption(readiness_desc)
        else:
            st.success(snap.get("bias", "?"))
            st.write(snap.get("structure_summary", "—"))
            m1, m2, m3 = st.columns(3)
            m1.metric("Bull OBs", snap.get("active_bullish_obs", 0))
            m2.metric("Bear OBs", snap.get("active_bearish_obs", 0))
            m3.metric("Swings", snap.get("swing_count", 0))
            st.caption(f"Price: {snap.get('current_price')}")
            bcount = buf.get('effective_bars', buf.get('bars_in_buffer', 0))
            st.progress(min(buf.get("pct_full", 0) / 100, 1.0))
            st.caption(f"Live buffer: {bcount} bars ({buf.get('pct_full', 0)}%) (incl. current minute)")

            # M5 progress (key for trends/structure)
            st.caption("M5 buffer (structure trends)")
            st.progress(min(m5_pct / 100, 1.0))
            st.caption(f"M5: {m5c}/{m5max} bars ({m5_pct}%)")

            st.caption(f"**Readiness:** {readiness}")
            st.caption(readiness_desc)

# =============================================================================
# SIGNAL PROGRESS / HISTORY
# =============================================================================
st.subheader("Signal Build-up History (what the engine has been saying)")

sig_symbol = st.selectbox("Filter by symbol", ["All"] + SYMBOLS, index=0)
sig_limit = st.slider("Signals to load", 20, 300, 80, step=10)

signals_df = fetch_recent_signals(None if sig_symbol == "All" else sig_symbol, sig_limit)
if not signals_df.empty:
    # Nice display columns
    disp = signals_df[["ts", "symbol", "timeframe", "signal", "score", "confidence", "setup", "structure_summary", "bias", "current_price", "total_confluence"]].copy()
    disp["ts"] = pd.to_datetime(disp["ts"])
    st.dataframe(disp, use_container_width=True, height=280)

    # Better progress chart: total_confluence over time (shows buildup even on HOLDs)
    # The signal "score" only goes non-zero when a full setup triggers.
    if "total_confluence" in disp.columns and len(disp) > 3:
        chart_df = disp[["ts", "total_confluence", "symbol", "signal"]].set_index("ts")
        st.line_chart(chart_df[["total_confluence"]], height=200)
        st.caption("Total confluence score over time (shows buildup of AMD + FIB + PA + LIQ even before a full setup triggers a non-HOLD signal. Threshold for setups is ~0.48)")
else:
    st.info("No signals logged yet. Run the EA + backend for a while.")

# =============================================================================
# TRADES SECTION
# =============================================================================
st.subheader("Executed Trades (reported by AutoTrader EA)")

trade_symbol = st.selectbox("Trades filter", ["All"] + SYMBOLS, index=0, key="trade_sym")
trades_df = fetch_trades(None if trade_symbol == "All" else trade_symbol)

if not trades_df.empty:
    st.dataframe(trades_df, use_container_width=True, height=220)
    # Quick stats
    if "r_multiple" in trades_df.columns:
        r = trades_df["r_multiple"].dropna()
        if len(r) > 0:
            c1, c2, c3 = st.columns(3)
            c1.metric("Trades", len(trades_df))
            c2.metric("Avg R", round(r.mean(), 2))
            c3.metric("Win rate (R>0)", f"{(r > 0).mean()*100:.0f}%")
else:
    st.info("No trades reported yet. When your EA takes trades, have it POST to /report-trade.")

# =============================================================================
# BUFFER + SYSTEM DETAILS
# =============================================================================
with st.expander("Live Buffer & System Details"):
    buf = fetch_buffer()
    st.json(buf)

    # Global readiness summary
    st.markdown("**Readiness Summary (based on M5 bars for structure/trends):**")
    if isinstance(buf, dict) and "all_symbols" in buf:
        for sym, b in buf.get("all_symbols", {}).items():
            m5c = b.get("m5_bars_in_buffer", 0)
            if m5c < 3:
                level = "🔴 Insufficient"
            elif m5c < 8:
                level = "🟡 Basic"
            elif m5c < 20:
                level = "🟢 Good"
            else:
                level = "✅ Strong"
            label = DISPLAY_NAMES.get(sym, sym)
            st.write(f"**{label}:** {level} ({m5c} M5 bars)")
    st.caption("The backend maintains a rolling 10080 M1 bar buffer per symbol for deep structure context (BOS/CHOCH, OBs, FVGs). Old bars drop off automatically. Readiness tiers: <3 M5=Insufficient, 3-7=Basic, 8-19=Good, 20+=Strong.")

# =============================================================================
# RUNNING TRADES (added at bottom as requested - keeps all previous UI intact)
# =============================================================================
st.subheader("📍 Running / Live Trades (Open Positions from EA)")

open_trades_df = fetch_open_trades()
if not open_trades_df.empty:
    # Show key columns if present
    cols = [c for c in ["ts", "symbol", "direction", "entry_price", "stop_loss", "tp1", "tp2", "volume_lots", "notes", "ticket"] if c in open_trades_df.columns]
    st.dataframe(open_trades_df[cols] if cols else open_trades_df, use_container_width=True, height=180)
    st.caption(f"{len(open_trades_df)} open trade(s) currently reported by the AutoTrader EA(s).")
else:
    st.info("No open/running trades reported yet. EA must POST to /report-trade when positions open.")

st.subheader("✅ Closed Trades - Where & Why (SL or TP Hit)")

# Reuse the existing trades fetch (already in scope from higher in the script)
# We enhance display here for close_reason without changing the earlier trades table
trades_for_close = fetch_trades()  # will use the one defined above if in scope, or re-fetch
if not trades_for_close.empty:
    # Prepare display with close reason
    if "close_reason" not in trades_for_close.columns:
        # fallback if backend didn't return it yet
        trades_for_close["close_reason"] = trades_for_close.get("notes", "").astype(str).str.lower().apply(
            lambda x: "sl" if "sl" in x else ("tp" if "tp" in x else "")
        )

    def make_reason_label(row):
        reason = str(row.get("close_reason", "")).lower() if pd.notna(row.get("close_reason")) else ""
        outcome = str(row.get("outcome", "")).lower()
        if "sl" in reason or (outcome and "loss" in outcome and "sl" not in reason):
            return "🔴 SL HIT"
        elif "tp" in reason or "take profit" in reason:
            return "🟢 TP HIT"
        elif reason:
            return "⚪ " + reason[:15]
        else:
            return ""

    trades_for_close = trades_for_close.copy()
    trades_for_close["_close_reason"] = trades_for_close.apply(make_reason_label, axis=1)

    # Show relevant columns
    display_cols = [c for c in ["ts", "symbol", "direction", "entry_price", "stop_loss", "tp1", "close_price", "_close_reason", "r_multiple", "outcome", "volume_lots"] if c in trades_for_close.columns]
    if display_cols:
        st.dataframe(trades_for_close[display_cols].tail(100), use_container_width=True, height=250)

    # Summary stats for SL vs TP
    sl_count = int(trades_for_close["_close_reason"].str.contains("SL", na=False).sum())
    tp_count = int(trades_for_close["_close_reason"].str.contains("TP", na=False).sum())
    st.caption(f"SL hits: {sl_count} | TP hits: {tp_count} (from all reported closed trades)")
else:
    st.info("No closed trades yet with reasons. When EA detects SL/TP hits (via history deals), it reports with close_reason.")

# =============================================================================
# AUTO REFRESH
# =============================================================================
if auto_refresh:
    time.sleep(refresh_sec)
    st.rerun()

st.caption("Tip: Leave this tab open during the trading day. The backend Windows service keeps running on weekdays even if you close the UI.")
