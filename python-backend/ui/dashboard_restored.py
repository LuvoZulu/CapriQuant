"""
CapriQuant Live Dashboard (Streamlit)

Launch with:
    streamlit run python-backend/ui/dashboard.py

Features:
- System status + live buffer fill (1440 M1 = strict 1 day target, from market not DB)
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
st.title("CapriQuant GÇó Real-time Structure & Signal Progress")

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

# =============================================================================
# TOP STATUS BAR
# =============================================================================
status = fetch_status()
col1, col2, col3, col4 = st.columns(4)
col1.metric("Backend", status.get("status", "down"))
col2.metric("Max M1 Buffer (1w+4d)", status.get("buffer_max_m1", 1440))
tracked = status.get("symbols_tracked", [])
col3.metric("Symbols tracked", len(tracked))
col4.metric("Last poll", datetime.utcnow().strftime("%H:%M:%S"))

st.caption(f"Backend: {BACKEND}  GÇó  Tracked: {', '.join(tracked) if tracked else 'none yet'}")

# =============================================================================
# LIVE SYMBOL CARDS + CURRENT STRUCTURE
# =============================================================================
st.subheader("Live Market Structure (from rolling buffer: 1 week + 4 days headroom before rewrite; post-off trend capped to 1440/1 day from market)")

card_cols = st.columns(len(SYMBOLS))
for i, sym in enumerate(SYMBOLS):
    snap = fetch_current(sym)
    with card_cols[i]:
        st.markdown(f"### {sym}")
        if "error" in snap or snap.get("status") == "insufficient_live_data":
            st.warning(snap.get("status", "No live data yet"))
            buf = snap.get("buffer", {})
            bcount = buf.get('effective_bars', buf.get('bars_in_buffer', 0))
            st.caption(f"Buffer: {bcount}/{buf.get('max_bars', 1440)} (incl. current minute)")
        else:
            st.success(snap.get("bias", "?"))
            st.write(snap.get("structure_summary", "GÇö"))
            m1, m2, m3 = st.columns(3)
            m1.metric("Bull OBs", snap.get("active_bullish_obs", 0))
            m2.metric("Bear OBs", snap.get("active_bearish_obs", 0))
            m3.metric("Swings", snap.get("swing_count", 0))
            st.caption(f"Price: {snap.get('current_price')}")
            buf = snap.get("buffer", {})
            bcount = buf.get('effective_bars', buf.get('bars_in_buffer', 0))
            st.progress(min(buf.get("pct_full", 0) / 100, 1.0))
            st.caption(f"Live buffer: {bcount} bars ({buf.get('pct_full', 0)}%) (incl. current minute)")

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
    st.caption("The backend maintains a rolling buffer (1 week of candles + space for another 4 days before rewriting). After system turns on, trend/structure checks only use last 1440 (1 day) from market/backfill. Displays show actual buffer fill.")

# =============================================================================
# AUTO REFRESH
# =============================================================================
if auto_refresh:
    time.sleep(refresh_sec)
    st.rerun()

st.caption("Tip: Leave this tab open during the trading day. The backend Windows service keeps running on weekdays even if you close the UI.")


