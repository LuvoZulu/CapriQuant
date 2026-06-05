"""
CapriQuant Enterprise Dashboard (Streamlit)

PROFESSIONAL / ENTERPRISE-GRADE TRADING OPERATIONS UI
- Real-time monitoring of structure engine, risk circuits, and live trades
- Kill switch / system mode (non-bypassable controls always prominent)
- Advanced performance attribution, risk analytics, and trade journaling
- Interactive visualizations (Plotly) for edge analysis
- Designed for prop firm / serious capital deployment: clarity, speed, safety, auditability

Launch:
    streamlit run python-backend/ui/dashboard.py
    or use python-backend/ui/run_ui.bat

Backend must be running (FastAPI on :8001, preferably as Windows service).
"""

import time
import requests
import pandas as pd
import streamlit as st
from datetime import datetime
import plotly.express as px
import plotly.graph_objects as go

# Enterprise styling - professional dark trading theme (similar to Bloomberg / TradingView)
CUSTOM_CSS = """
<style>
    .stApp {
        background-color: #0e1117;
        color: #fafafa;
    }
    .stMetric {
        background-color: #1a1f2e;
        border-radius: 8px;
        padding: 8px;
        border: 1px solid #2a3142;
    }
    .stMetric label {
        color: #a0a0a0 !important;
        font-size: 0.75rem !important;
    }
    .stMetric .metric-value {
        font-size: 1.35rem !important;
        font-weight: 600;
    }
    .stDataFrame {
        border: 1px solid #2a3142;
        border-radius: 6px;
    }
    .stTabs [data-baseweb="tab-list"] {
        background-color: #161b26;
        border-radius: 8px;
        padding: 4px;
    }
    .stTabs [data-baseweb="tab"] {
        color: #c0c0c0;
        padding: 8px 16px;
    }
    .stTabs [aria-selected="true"] {
        background-color: #1f2738;
        color: #00d4ff;
        border-radius: 6px;
    }
    .stButton button {
        border-radius: 6px;
        font-weight: 500;
    }
    .stButton button[kind="primary"] {
        background-color: #c62828;
        border-color: #c62828;
    }
    .stAlert {
        border-radius: 8px;
    }
    .enterprise-header {
        background: linear-gradient(90deg, #0e1117 0%, #1a1f2e 100%);
        padding: 12px 20px;
        border-bottom: 2px solid #00d4ff;
        margin-bottom: 16px;
        border-radius: 0 0 8px 8px;
    }
    .pro-card {
        background-color: #161b26;
        border: 1px solid #2a3142;
        border-radius: 10px;
        padding: 12px;
        margin-bottom: 12px;
    }
    .green { color: #00c853 !important; }
    .red { color: #ff5252 !important; }
    .yellow { color: #ffab00 !important; }
    .caption { color: #888 !important; font-size: 0.8rem; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# =============================================================================
# CONFIG
# =============================================================================
BACKEND = "http://127.0.0.1:8001"   # The FastAPI backend (service) - this is where the UI fetches data from. Backend runs on 8001.
POLL_SECONDS = 5
SYMBOLS = ["US30", "USTEC", "DE30", "XAUUSD"]  # extend as you add more

# Run the UI (Streamlit) with:
#   python -m streamlit run ui/dashboard.py --server.address 127.0.0.1
# Or double-click: python-backend/ui/run_ui.bat
# (Streamlit UI itself can run on any port - default is 8501. It fetches data from the backend on 8001)

st.set_page_config(
    page_title="CapriQuant Enterprise • Live Ops Dashboard",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        'Get Help': 'https://github.com/LuvoZulu/CapriQuant',
        'Report a bug': "mailto:support@capriquant.example",
        'About': "# CapriQuant Enterprise Trading Platform\nProfessional SMC Structure Engine for Prop Capital"
    }
)

# Enterprise header
st.markdown("""
<div class="enterprise-header">
    <div style="display:flex; align-items:center; justify-content:space-between;">
        <div>
            <span style="font-size:1.8rem; font-weight:700; color:#00d4ff;">CAPRIQUANT</span>
            <span style="font-size:1.1rem; color:#888; margin-left:12px;">Enterprise Trading Operations</span>
        </div>
        <div style="text-align:right; font-size:0.8rem; color:#888;">
            Real-time Structure • Risk Circuits • Edge Attribution<br/>
            <span style="color:#00c853;">●</span> Live from market buffer (not DB)
        </div>
    </div>
</div>
""", unsafe_allow_html=True)

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

# Alerts banner (new rec)
try:
    alerts_resp = requests.get(f"{BACKEND}/api/alerts", timeout=2).json()
    alerts = alerts_resp.get("alerts", [])
    if alerts:
        st.error("⚠️ ACTIVE ALERTS")
        for a in alerts:
            lvl = a.get("level", "info").upper()
            st.write(f"**{lvl}** [{a.get('type')}]: {a.get('msg')}")
except:
    pass

# Sidebar - Enterprise controls
st.sidebar.header("⚙️ Dashboard Controls")
auto_refresh = st.sidebar.checkbox("Auto-refresh (live feel)", value=True)
refresh_sec = st.sidebar.slider("Poll interval (seconds)", 2, 30, POLL_SECONDS, step=1)

col_a, col_b = st.sidebar.columns(2)
with col_a:
    if st.sidebar.button("🔄 Force Refresh", use_container_width=True):
        st.rerun()
with col_b:
    if st.sidebar.button("📥 Export All Data", use_container_width=True):
        st.toast("Export feature - dataframes available below for download", icon="📊")

st.sidebar.markdown("---")
st.sidebar.caption("**Enterprise Notes**")
st.sidebar.caption("• Kill switch always visible & non-bypassable")
st.sidebar.caption("• All data direct from live market buffer")
st.sidebar.caption("• Structure engine + hard risk circuits")
st.sidebar.caption("• For prop firm / institutional deployment")

# Main content organized into professional tabs for enterprise UX
tab_overview, tab_structure, tab_risk, tab_performance, tab_trades, tab_control = st.tabs([
    "📊 Live Overview",
    "🔬 Structure Deep Dive",
    "🛡️ Risk & Alerts",
    "📈 Performance & Attribution",
    "📋 Trade Journal",
    "🚨 System Control"
])

with tab_overview:
    # =============================================================================
    # TOP STATUS BAR (Enterprise metrics row)
    # =============================================================================
    status = fetch_status()
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Backend Status", status.get("status", "down").upper())
    m2.metric("M1 Buffer Cap", f"{status.get('buffer_max_m1', 15840):,}")
    tracked = status.get("symbols_tracked", [])
    m3.metric("Symbols Tracked", len(tracked))
    m4.metric("Catch-up Window", f"{status.get('catchup_max_hours', 24)}h max")
    m5.metric("Last Update", datetime.utcnow().strftime("%H:%M:%S"))

    st.caption(f"**Backend:** {BACKEND}  •  **Tracked:** {', '.join(tracked) if tracked else 'none yet'}  •  Data source: **Direct market buffer** (1w+4d headroom, post-off capped)")

    # =============================================================================
    # LIVE SYMBOL CARDS + CURRENT STRUCTURE (Pro cards)
    # =============================================================================
    st.subheader("Live Market Structure — Readiness per Symbol")
    st.caption("Buffer: 1 week + 4 days headroom (15840 M1). Post-off: only last 1440 M1 from market used for trend/structure. M5 primary for decisions.")

    card_cols = st.columns(len(SYMBOLS))
    for i, sym in enumerate(SYMBOLS):
        snap = fetch_current(sym)
        with card_cols[i]:
            st.markdown(f"**{sym}**")
            buf = snap.get("buffer", {}) if isinstance(snap, dict) else {}
            m5c = buf.get("m5_bars_in_buffer", 0)
            m5max = buf.get("max_m5_bars", 3168)
            m5_pct = buf.get("m5_pct_full", 0)

            # Enterprise readiness tiers
            if m5c < 3:
                readiness = "🔴 INSUFFICIENT"
                color = "red"
            elif m5c < 8:
                readiness = "🟡 BASIC"
                color = "yellow"
            elif m5c < 20:
                readiness = "🟢 GOOD"
                color = "green"
            else:
                readiness = "✅ STRONG"
                color = "green"

            if "error" in snap or snap.get("status") == "insufficient_live_data":
                st.warning("Insufficient live data")
                bcount = buf.get('effective_bars', buf.get('bars_in_buffer', 0))
                st.caption(f"Buffer: {bcount} / {buf.get('max_bars', 15840)}")
                st.caption(f"Readiness: {readiness}")
            else:
                bias = snap.get("bias", "?")
                bias_class = "green" if "BULL" in bias else ("red" if "BEAR" in bias else "")
                st.markdown(f"<span class='{bias_class}' style='font-size:1.4rem; font-weight:700;'>{bias}</span>", unsafe_allow_html=True)
                st.write(snap.get("structure_summary", "—"))

                o1, o2, o3 = st.columns(3)
                o1.metric("Bull OBs", snap.get("active_bullish_obs", 0))
                o2.metric("Bear OBs", snap.get("active_bearish_obs", 0))
                o3.metric("Swings", snap.get("swing_count", 0))

                price = snap.get('current_price')
                if price:
                    st.metric("Price", f"{price:.2f}")

                # Buffer progress
                st.progress(min(buf.get("pct_full", 0) / 100, 1.0))
                bcount = buf.get('effective_bars', buf.get('bars_in_buffer', 0))
                st.caption(f"M1 Buffer: {bcount} ({buf.get('pct_full', 0)}%)")

                st.progress(min(m5_pct / 100, 1.0))
                st.caption(f"M5 (trend primary): {m5c}/{m5max} ({m5_pct}%)")

                st.caption(f"**Readiness: {readiness}**")
                st.caption("M5 ≥20 = full context for high-quality setups")

# End of Live Overview tab content (enterprise polished version above)

# Put the old content into the other tabs with significant enterprise upgrades
# (re-using the fetch_* helpers defined earlier in the file)

with tab_structure:
    st.subheader("🔬 Structure Build-up & Signal History")
    st.caption("See how AMD + Fib + PA + Liquidity + CRT confluence builds over time. This is the 'progress bar' toward a high-quality setup.")

    sig_symbol = st.selectbox("Filter signals by symbol", ["All"] + SYMBOLS, index=0, key="struct_sym")
    sig_limit = st.slider("Number of signals", 30, 400, 120, step=10, key="struct_lim")

    signals_df = fetch_recent_signals(None if sig_symbol == "All" else sig_symbol, sig_limit)
    if not signals_df.empty:
        disp = signals_df[[c for c in ["ts", "symbol", "timeframe", "signal", "score", "confidence", "setup", "structure_summary", "bias", "current_price", "total_confluence"] if c in signals_df.columns]].copy()
        disp["ts"] = pd.to_datetime(disp["ts"])
        st.dataframe(disp, use_container_width=True, height=320)

        if "total_confluence" in disp.columns and len(disp) > 5:
            fig = px.line(disp, x="ts", y="total_confluence", color="symbol" if "symbol" in disp.columns else None,
                          title="Confluence Build-up Over Time (key leading indicator)",
                          labels={"total_confluence": "Total Confluence", "ts": "Time"})
            fig.update_layout(height=320, template="plotly_dark", hovermode="x unified")
            st.plotly_chart(fig, use_container_width=True)
            st.caption("Confluence (AMD + FIB + PA + LIQ + CRT) builds before a full setup triggers a non-HOLD. Threshold ~0.48 in config.")
    else:
        st.info("No signals yet — start the EA + backend to populate history.")

with tab_risk:
    st.subheader("🛡️ Risk Circuits, Alerts & Daily Controls")
    st.caption("Hard, non-bypassable risk layers (streak, daily loss) + live alerts. This is what keeps prop capital safe.")

    # Re-show kill switch in this tab for convenience (already prominent in header area)
    st.markdown("**Quick System Mode Controls (same as top)**")
    c1, c2, c3 = st.columns(3)
    if c1.button("🚨 EMERGENCY FLATTEN ALL", type="primary", use_container_width=True, key="risk_flat"):
        try:
            r = requests.post(f"{BACKEND}/api/control", json={"action": "flatten_all"}, timeout=5)
            st.error(str(r.json()))
        except Exception as e:
            st.error(str(e))
    if c2.button("⏸️ PAUSE (signals → HOLD)", use_container_width=True, key="risk_pause"):
        try:
            r = requests.post(f"{BACKEND}/api/control", json={"action": "pause"}, timeout=5)
            st.warning(str(r.json()))
        except Exception as e:
            st.error(str(e))
    if c3.button("▶️ RESUME TRADING", use_container_width=True, key="risk_resume"):
        try:
            r = requests.post(f"{BACKEND}/api/control", json={"action": "resume"}, timeout=5)
            st.success(str(r.json()))
        except Exception as e:
            st.error(str(e))

    try:
        alerts = fetch_json("/api/alerts").get("alerts", [])
        if alerts:
            st.error("ACTIVE ALERTS — ACTION REQUIRED")
            for a in alerts:
                st.write(f"**{a.get('level','INFO').upper()}** — {a.get('type')}: {a.get('msg')}")
        else:
            st.success("No active risk or system alerts")
    except:
        st.warning("Could not fetch alerts")

    st.markdown("---")
    st.subheader("Open Positions + Management Suggestions")
    open_df = fetch_open_trades()
    if not open_df.empty:
        st.dataframe(open_df, use_container_width=True, height=200)
        if "management" in open_df.columns:
            for _, row in open_df.iterrows():
                if pd.notna(row.get("management")):
                    m = row["management"]
                    if isinstance(m, dict):
                        action = m.get("action", "")
                        color = "🟢" if "BE" in action or "TRAIL" in action else "🔴"
                        st.write(f"{color} **{row.get('symbol')}** Ticket {row.get('ticket')}: {action} → new SL {m.get('new_sl')} | {m.get('reason')}")
    else:
        st.info("No open trades reported.")

with tab_performance:
    st.subheader("📈 Performance Attribution & Edge Analysis (Enterprise Analytics)")
    st.caption("This is what separates hobby systems from prop-firm grade operations. Understand *why* you win or lose.")

    trades_df = fetch_trades(limit=300)
    if not trades_df.empty and "r_multiple" in trades_df.columns:
        r = trades_df["r_multiple"].fillna(0).astype(float)

        # Key enterprise metrics
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Closed Trades", len(trades_df))
        c2.metric("Expectancy (R)", round(r.mean(), 3))
        c3.metric("Win Rate (R>0)", f"{(r > 0).mean()*100:.1f}%")
        pf = (r[r > 0].sum() / abs(r[r <= 0].sum())) if (r <= 0).any() else 99
        c4.metric("Profit Factor", round(pf, 2))

        # R distribution (interactive)
        fig_hist = px.histogram(trades_df, x="r_multiple", nbins=25, title="R-Multiple Distribution",
                                color_discrete_sequence=["#00d4ff"])
        fig_hist.update_layout(template="plotly_dark", height=280)
        st.plotly_chart(fig_hist, use_container_width=True)

        # Cumulative equity curve
        cum = r.cumsum()
        fig_cum = go.Figure()
        fig_cum.add_trace(go.Scatter(x=list(range(len(cum))), y=cum, mode="lines", name="Cumulative R", line=dict(color="#00c853", width=2)))
        fig_cum.update_layout(title="Cumulative Realized R (Equity Curve in R units)", template="plotly_dark", height=280, xaxis_title="Trade #", yaxis_title="Cumulative R")
        st.plotly_chart(fig_cum, use_container_width=True)

        # By symbol and setup attribution (key for tuning the world's best prop strategies)
        if "symbol" in trades_df.columns:
            by_sym = trades_df.groupby("symbol")["r_multiple"].agg(["count", "mean", "sum"]).round(3)
            by_sym.columns = ["Trades", "Avg R", "Total R"]
            st.write("**Performance by Symbol**")
            st.dataframe(by_sym, use_container_width=True)

        if "setup" in trades_df.columns and trades_df["setup"].notna().any():
            by_setup = trades_df[trades_df["setup"].notna()].groupby("setup")["r_multiple"].agg(["count", "mean", "sum"]).round(3)
            by_setup.columns = ["Trades", "Avg R", "Total R"]
            st.write("**Performance by Setup (most important for edge refinement)**")
            st.dataframe(by_setup.sort_values("Total R", ascending=False), use_container_width=True)

        # Simple download
        csv = trades_df.to_csv(index=False).encode()
        st.download_button("⬇️ Download Trade Log (CSV)", csv, "capriquant_trades.csv", "text/csv")
    else:
        st.info("No trade data with R-multiples yet for attribution analysis.")

with tab_trades:
    st.subheader("📋 Full Trade Journal + Close Reason Analysis")
    st.caption("SL vs TP attribution + setup performance = the data you need to become one of the world's best prop traders.")

    trades_for_close = fetch_trades(limit=500)
    if not trades_for_close.empty:
        # Reuse/enhance the close reason logic from original
        if "close_reason" not in trades_for_close.columns:
            trades_for_close["close_reason"] = trades_for_close.get("notes", pd.Series([""]*len(trades_for_close))).astype(str).str.lower().apply(
                lambda x: "sl" if "sl" in x else ("tp" if "tp" in x else "")
            )

        def make_reason_label(row):
            reason = str(row.get("close_reason", "")).lower() if pd.notna(row.get("close_reason")) else ""
            outcome = str(row.get("outcome", "")).lower()
            if "sl" in reason or (outcome and "loss" in outcome and "sl" not in reason):
                return "🔴 SL"
            elif "tp" in reason or "take profit" in reason:
                return "🟢 TP"
            return reason[:12] if reason else ""

        trades_for_close = trades_for_close.copy()
        trades_for_close["_reason"] = trades_for_close.apply(make_reason_label, axis=1)

        display_cols = [c for c in ["ts", "symbol", "direction", "entry_price", "stop_loss", "close_price", "_reason", "r_multiple", "setup", "outcome"] if c in trades_for_close.columns]
        st.dataframe(trades_for_close[display_cols].tail(150), use_container_width=True, height=320)

        sl = int(trades_for_close["_reason"].str.contains("SL", na=False).sum())
        tp = int(trades_for_close["_reason"].str.contains("TP", na=False).sum())
        st.metric("SL Hits vs TP Hits (all time)", f"{sl} SL  |  {tp} TP")

        if "setup" in trades_for_close.columns:
            st.write("**Closed Trades by Setup**")
            st.dataframe(trades_for_close[trades_for_close["setup"].notna()].groupby("setup")["_reason"].value_counts().unstack(fill_value=0), use_container_width=True)

        csv = trades_for_close.to_csv(index=False).encode()
        st.download_button("Download Full Journal CSV", csv, "capriquant_full_journal.csv")
    else:
        st.info("No closed trades reported yet.")

with tab_control:
    st.subheader("🚨 Full System Control & Health")
    st.warning("These controls are non-bypassable and affect every EA connected to this backend.")

    # Full controls (duplicated here for the dedicated tab)
    mode_resp = {}
    try:
        mode_resp = requests.get(f"{BACKEND}/api/system-mode", timeout=2).json()
    except:
        pass
    current_mode = mode_resp.get("mode", "trading")
    st.metric("Current System Mode", current_mode.upper())

    cols = st.columns(4)
    with cols[0]:
        if st.button("FLATTEN ALL + PAUSE", type="primary", use_container_width=True):
            requests.post(f"{BACKEND}/api/control", json={"action": "flatten_all"})
            st.rerun()
    with cols[1]:
        if st.button("PAUSE ONLY"):
            requests.post(f"{BACKEND}/api/control", json={"action": "pause"})
            st.rerun()
    with cols[2]:
        if st.button("RESUME TRADING"):
            requests.post(f"{BACKEND}/api/control", json={"action": "resume"})
            st.rerun()
    with cols[3]:
        if st.button("Refresh"):
            st.rerun()

    st.markdown("**Live Buffer Health**")
    try:
        st.json(fetch_json("/debug/live-buffer"))
    except:
        st.info("Buffer data unavailable")

    st.markdown("**Prometheus-style Metrics**")
    try:
        metrics = requests.get(f"{BACKEND}/metrics", timeout=2).text
        st.code(metrics, language="text")
    except:
        pass

# =============================================================================
# GLOBAL AUTO REFRESH (applies to current tab)
# =============================================================================
if auto_refresh:
    time.sleep(refresh_sec)
    st.rerun()

st.caption("Enterprise Dashboard • Leave open during session • Backend service handles 24/5 operation • All risk layers are hard and non-bypassable")
