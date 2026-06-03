"""
CapriQuant Live Dashboard (Streamlit) — 4 symbols only. (CLEAN + TRADE CLOSE TRACKING)

See previous full content for the enhanced version with Open Trades / Closed Trades + SL/TP reasons.
This is a restored clean version with the feature.
"""
import time
import requests
import pandas as pd
import streamlit as st
from datetime import datetime

BACKEND = "http://127.0.0.1:8001"
POLL_SECONDS = 5
UI_SYMBOLS = ["XAUUSD", "DE30", "USTEC", "US30"]
DISPLAY_NAMES = {"XAUUSD": "XAUUSD", "DE30": "GER30", "USTEC": "NAS100", "US30": "US30"}

st.set_page_config(page_title="CapriQuant - Live + Trades", layout="wide")
st.title("CapriQuant • Structure + Live Trade Close Tracker (SL/TP)")

st.sidebar.header("Controls")
auto_refresh = st.sidebar.checkbox("Auto-refresh", value=True)
refresh_sec = st.sidebar.slider("Refresh (s)", 3, 30, POLL_SECONDS)
if st.sidebar.button("Force now"): st.rerun()

def fetch_json(p, params=None):
    try:
        return requests.get(f"{BACKEND}{p}", params=params or {}, timeout=5).json()
    except Exception as e: return {"error": str(e)}

def fetch_trades(sym=None, lim=200):
    data = fetch_json("/api/trades", {"symbol": sym, "limit": lim})
    return pd.DataFrame(data) if isinstance(data, list) else pd.DataFrame()

def fetch_open(sym=None, lim=50):
    data = fetch_json("/api/open-trades", {"symbol": sym, "limit": lim})
    return pd.DataFrame(data) if isinstance(data, list) else pd.DataFrame()

# Status
st.subheader("System")
st.json(fetch_json("/api/health"))

# Open Trades
st.subheader("📍 Open Trades (live from EA reports)")
ot = fetch_open()
if not ot.empty:
    st.dataframe(ot, use_container_width=True)
else:
    st.info("No open trades reported yet.")

# Closed with reasons
st.subheader("✅ Closed Trades — Close Reason (SL red / TP green)")
ct = fetch_trades()
if not ct.empty:
    if "close_reason" not in ct.columns:
        ct["close_reason"] = ct.get("notes", "")
    def badge(r):
        r = str(r).lower() if pd.notna(r) else ""
        if "sl" in r: return "🔴 SL"
        if "tp" in r: return "🟢 TP"
        return "⚪ " + r[:12]
    ct["_reason"] = ct["close_reason"].apply(badge)
    st.dataframe(ct[["ts","symbol","direction","entry_price","_reason","r_multiple","outcome"]].tail(50), use_container_width=True, height=300)
    # summary
    slc = ct["close_reason"].astype(str).str.lower().str.contains("sl").sum()
    tpc = ct["close_reason"].astype(str).str.lower().str.contains("tp").sum()
    st.caption(f"SL hits: {slc} | TP hits: {tpc}")
else:
    st.info("No closed trades. EA must report status=closed + close_reason on SL/TP.")

if auto_refresh:
    time.sleep(refresh_sec)
    st.rerun()
