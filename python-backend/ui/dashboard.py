"""
CapriQuant Enterprise Dashboard (Streamlit) — v3.0
===================================================
ENTERPRISE TRADING OPERATIONS CENTER

Professional SMC Structure Engine Monitor
- Real-time market structure, risk circuits, trade lifecycle
- Kill switch always visible and non-bypassable (unchanged from v2)
- Session-aware clock with London / NY / Asia badges
- Live connection health indicator
- Risk circuit panel wired to real /api/system-status data
- Signal engine counters (BUY / SELL / HOLD / vetoes) in sidebar
- New ⚡ Lifecycle tab from /lifecycle/status
- Data quality panel from quality_issues
- Performance: Sharpe approximation + side-by-side charts
- All original backend calls and endpoints preserved exactly

Launch:
    streamlit run python-backend/ui/dashboard.py
    or use python-backend/ui/run_ui.bat

Backend must be running (FastAPI on :8001).
"""

import time
import requests
import pandas as pd
import streamlit as st
from datetime import datetime
import plotly.express as px
import plotly.graph_objects as go

# ─── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CapriQuant • Trading Operations",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "Get Help": "https://github.com/LuvoZulu/CapriQuant",
        "Report a bug": "mailto:support@capriquant.example",
        "About": "# CapriQuant v3.0\nEnterprise SMC Trading Operations Center",
    },
)

# ─── Google Fonts ─────────────────────────────────────────────────────────────
st.markdown(
    """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=JetBrains+Mono:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
""",
    unsafe_allow_html=True,
)

# ─── CSS ──────────────────────────────────────────────────────────────────────
CUSTOM_CSS = """
<style>
/* ── Design tokens ── */
:root {
    --font-ui:   'Outfit', sans-serif;
    --font-mono: 'JetBrains Mono', monospace;

    /* Surfaces */
    --bg-base:     #060c18;
    --bg-surface:  #0c1526;
    --bg-raised:   #101d32;
    --bg-elevated: #162540;

    /* Borders */
    --border-dim:    #162238;
    --border-mid:    #1e3558;
    --border-bright: #285080;

    /* Brand accent */
    --accent:       #3d9eff;
    --accent-glow:  rgba(61,158,255,0.18);
    --accent-dim:   rgba(61,158,255,0.08);

    /* Status palette */
    --green:      #00e676;
    --green-bg:   rgba(0,230,118,0.08);
    --red:        #ff4d6a;
    --red-bg:     rgba(255,77,106,0.08);
    --yellow:     #ffd740;
    --yellow-bg:  rgba(255,215,64,0.08);
    --purple:     #b388ff;
    --purple-bg:  rgba(179,136,255,0.08);

    /* Text */
    --text-hi:  #ddeeff;
    --text-mid: #6e8aaa;
    --text-lo:  #2e4560;
}

/* ── Base ── */
.stApp {
    background-color: var(--bg-base) !important;
    font-family: var(--font-ui) !important;
    color: var(--text-hi) !important;
}
.main .block-container {
    padding-top: 0 !important;
    max-width: 100% !important;
    padding-left: 1rem !important;
    padding-right: 1rem !important;
}
footer { display: none !important; }
#MainMenu { visibility: hidden !important; }

/* ── Sidebar ── */
[data-testid="stSidebar"] {
    background-color: var(--bg-surface) !important;
    border-right: 1px solid var(--border-dim) !important;
}
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] .stMarkdown { color: var(--text-mid) !important; }

/* ── Metric cards ── */
[data-testid="stMetric"] {
    background: var(--bg-raised) !important;
    border: 1px solid var(--border-dim) !important;
    border-top: 2px solid var(--accent) !important;
    border-radius: 10px !important;
    padding: 14px 16px !important;
}
[data-testid="stMetric"] label {
    font-family: var(--font-ui) !important;
    font-size: 0.67rem !important;
    font-weight: 700 !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    color: var(--text-mid) !important;
}
[data-testid="stMetric"] [data-testid="stMetricValue"] {
    font-family: var(--font-mono) !important;
    font-size: 1.35rem !important;
    font-weight: 600 !important;
    color: var(--text-hi) !important;
}
[data-testid="stMetric"] [data-testid="stMetricDelta"] {
    font-family: var(--font-mono) !important;
    font-size: 0.72rem !important;
}

/* ── Tabs ── */
[data-baseweb="tab-list"] {
    background: var(--bg-surface) !important;
    border: 1px solid var(--border-dim) !important;
    border-radius: 12px !important;
    padding: 4px !important;
    gap: 2px !important;
}
[data-baseweb="tab"] {
    font-family: var(--font-ui) !important;
    font-size: 0.77rem !important;
    font-weight: 500 !important;
    color: var(--text-lo) !important;
    padding: 7px 16px !important;
    border-radius: 8px !important;
    transition: all 0.2s;
}
[aria-selected="true"] {
    background: var(--bg-elevated) !important;
    color: var(--accent) !important;
    border: 1px solid var(--border-mid) !important;
    font-weight: 600 !important;
}

/* ── Buttons ── */
.stButton button {
    font-family: var(--font-ui) !important;
    font-weight: 600 !important;
    font-size: 0.78rem !important;
    letter-spacing: 0.05em !important;
    border-radius: 8px !important;
    border: 1px solid var(--border-mid) !important;
    background: var(--bg-raised) !important;
    color: var(--text-hi) !important;
    transition: all 0.15s ease !important;
}
.stButton button:hover {
    background: var(--bg-elevated) !important;
    border-color: var(--accent) !important;
    color: var(--accent) !important;
}
.stButton button[kind="primary"] {
    background: linear-gradient(135deg, #6b0000, #b71c1c) !important;
    border-color: #d32f2f !important;
    color: #fff !important;
    box-shadow: 0 0 14px rgba(211,47,47,0.3) !important;
}
.stButton button[kind="primary"]:hover {
    box-shadow: 0 0 22px rgba(211,47,47,0.55) !important;
    color: #fff !important;
}

/* ── DataFrames ── */
[data-testid="stDataFrame"] {
    border: 1px solid var(--border-dim) !important;
    border-radius: 10px !important;
    overflow: hidden;
}

/* ── Form controls ── */
.stSelectbox label, .stSlider label, .stCheckbox label {
    font-family: var(--font-ui) !important;
    font-size: 0.7rem !important;
    font-weight: 700 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.08em !important;
    color: var(--text-mid) !important;
}

/* ── Code / JSON ── */
.stCode, [data-testid="stJson"] {
    background: var(--bg-surface) !important;
    border: 1px solid var(--border-dim) !important;
    border-radius: 8px !important;
    font-family: var(--font-mono) !important;
    font-size: 0.72rem !important;
}

/* ── Progress bars ── */
.stProgress > div > div > div {
    background: linear-gradient(90deg, var(--accent), var(--green)) !important;
}

/* ── Alerts ── */
.stAlert { border-radius: 10px !important; font-family: var(--font-ui) !important; }

/* ── Divider ── */
hr { border-color: var(--border-dim) !important; }

/* ═══════════════════════════════════════════════
   CUSTOM COMPONENT CLASSES
═══════════════════════════════════════════════ */

/* Header */
.cq-header {
    background: linear-gradient(180deg, var(--bg-surface) 0%, var(--bg-base) 100%);
    border-bottom: 1px solid var(--border-dim);
    padding: 14px 24px 14px;
    margin-bottom: 0;
    position: relative;
    overflow: hidden;
}
.cq-header::after {
    content: '';
    position: absolute;
    bottom: 0; left: 0;
    width: 100%; height: 1px;
    background: linear-gradient(90deg, transparent 0%, var(--accent) 40%, var(--green) 60%, transparent 100%);
    animation: header-sweep 5s ease-in-out infinite;
}
@keyframes header-sweep {
    0%   { opacity: 0.2; transform: translateX(-60%); }
    50%  { opacity: 1; }
    100% { opacity: 0.2; transform: translateX(60%); }
}

.cq-wordmark {
    font-family: var(--font-mono);
    font-size: 1.45rem;
    font-weight: 600;
    letter-spacing: 0.18em;
    color: var(--text-hi);
}
.cq-wordmark .accent { color: var(--accent); }
.cq-subtitle {
    font-family: var(--font-ui);
    font-size: 0.68rem;
    font-weight: 400;
    color: var(--text-lo);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-top: 3px;
}

/* Kill switch bar */
.cq-ks-bar {
    background: linear-gradient(90deg, rgba(211,47,47,0.07), transparent);
    border: 1px solid rgba(211,47,47,0.22);
    border-radius: 10px;
    padding: 10px 16px;
    margin: 10px 0 6px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}

/* Badges */
.cq-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 10px;
    border-radius: 20px;
    font-family: var(--font-mono);
    font-size: 0.66rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    border: 1px solid currentColor;
}
.badge-live    { color: var(--green);  background: var(--green-bg); }
.badge-dead    { color: var(--red);    background: var(--red-bg);   }
.badge-trading { color: var(--green);  background: var(--green-bg); }
.badge-paused  { color: var(--yellow); background: var(--yellow-bg); }
.badge-flatten { color: var(--red);    background: var(--red-bg);
                 animation: flash 0.6s ease-in-out infinite alternate; }
.badge-unknown { color: var(--text-mid); background: transparent; }

@keyframes flash { from { opacity: 1; } to { opacity: 0.4; } }

.cq-session {
    padding: 3px 9px;
    border-radius: 5px;
    font-family: var(--font-mono);
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
}

/* Live pulse dot */
.pulse {
    display: inline-block;
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 0 0 rgba(0,230,118,0.4);
    animation: pulse-anim 2.2s ease-out infinite;
}
.pulse-red {
    background: var(--red);
    box-shadow: 0 0 0 0 rgba(255,77,106,0.4);
    animation: pulse-red-anim 2.2s ease-out infinite;
}
@keyframes pulse-anim {
    0%   { box-shadow: 0 0 0 0 rgba(0,230,118,0.5); }
    70%  { box-shadow: 0 0 0 9px rgba(0,230,118,0); }
    100% { box-shadow: 0 0 0 0 rgba(0,230,118,0); }
}
@keyframes pulse-red-anim {
    0%   { box-shadow: 0 0 0 0 rgba(255,77,106,0.5); }
    70%  { box-shadow: 0 0 0 9px rgba(255,77,106,0); }
    100% { box-shadow: 0 0 0 0 rgba(255,77,106,0); }
}

/* Cards */
.cq-card {
    background: var(--bg-raised);
    border: 1px solid var(--border-dim);
    border-radius: 12px;
    padding: 14px 16px;
    margin-bottom: 10px;
    transition: border-color 0.2s;
}
.cq-card:hover { border-color: var(--border-mid); }
.cq-card-accent  { border-left: 3px solid var(--accent) !important; }
.cq-card-danger  { border-left: 3px solid var(--red) !important;    background: linear-gradient(90deg, var(--red-bg), var(--bg-raised)) !important; }
.cq-card-success { border-left: 3px solid var(--green) !important;  background: linear-gradient(90deg, var(--green-bg), var(--bg-raised)) !important; }
.cq-card-warn    { border-left: 3px solid var(--yellow) !important; background: linear-gradient(90deg, var(--yellow-bg), var(--bg-raised)) !important; }
.cq-card-purple  { border-left: 3px solid var(--purple) !important; background: linear-gradient(90deg, var(--purple-bg), var(--bg-raised)) !important; }

/* Symbol cards */
.sym-card {
    background: var(--bg-raised);
    border: 1px solid var(--border-dim);
    border-radius: 12px;
    padding: 14px;
    margin-bottom: 4px;
    min-height: 160px;
}
.sym-label {
    font-family: var(--font-mono);
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--text-mid);
    letter-spacing: 0.1em;
}
.sym-bias {
    font-family: var(--font-mono);
    font-size: 1.9rem;
    font-weight: 700;
    margin: 6px 0 2px;
    line-height: 1;
}
.sym-price {
    font-family: var(--font-mono);
    font-size: 1.05rem;
    color: var(--text-hi);
    margin-bottom: 6px;
}
.sym-summary {
    font-family: var(--font-ui);
    font-size: 0.68rem;
    color: var(--text-lo);
    margin-bottom: 8px;
    line-height: 1.4;
}

/* Section titles */
.cq-section {
    font-family: var(--font-ui);
    font-size: 0.62rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--text-lo);
    padding-bottom: 6px;
    border-bottom: 1px solid var(--border-dim);
    margin: 18px 0 10px;
}

/* Stat rows (sidebar) */
.stat-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 5px 0;
    border-bottom: 1px solid var(--border-dim);
    font-size: 0.77rem;
}
.stat-row:last-child { border-bottom: none; }
.stat-lbl { font-family: var(--font-ui);  color: var(--text-mid); font-size: 0.72rem; }
.stat-val { font-family: var(--font-mono); color: var(--text-hi);  font-size: 0.77rem; font-weight: 500; }

/* Color helpers */
.bull  { color: var(--green)  !important; }
.bear  { color: var(--red)    !important; }
.warn  { color: var(--yellow) !important; }
.dim   { color: var(--text-lo) !important; }
.hi    { color: var(--text-hi) !important; }
.acc   { color: var(--accent) !important; }

/* Lifecycle trade card */
.lc-card {
    background: var(--bg-raised);
    border: 1px solid var(--border-dim);
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
}
.lc-meta { font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-mid); }
.lc-id   { font-family: var(--font-mono); font-size: 0.88rem; font-weight: 600; color: var(--text-hi); }
.lc-rr   { font-family: var(--font-mono); font-size: 1.15rem; font-weight: 700; }
.lc-tiny { font-family: var(--font-ui); font-size: 0.6rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-lo); }
.be-pill {
    display: inline-block;
    padding: 1px 7px;
    border-radius: 4px;
    font-family: var(--font-mono);
    font-size: 0.6rem;
    font-weight: 700;
    background: var(--green-bg);
    color: var(--green);
    border: 1px solid var(--green);
    margin-left: 6px;
}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# =============================================================================
# CONFIG
# =============================================================================
BACKEND = "http://127.0.0.1:8001"
POLL_SECONDS = 5
SYMBOLS = ["US30", "USTEC", "DE30", "XAUUSD"]

# Plotly base layout applied to every chart
CQ_LAYOUT = dict(
    template="plotly_dark",
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="#0c1526",
    font=dict(family="JetBrains Mono, monospace", color="#6e8aaa", size=11),
    title_font=dict(family="Outfit, sans-serif", color="#ddeeff", size=13, weight=600),
    xaxis=dict(gridcolor="#162238", zerolinecolor="#162238", tickfont_family="JetBrains Mono"),
    yaxis=dict(gridcolor="#162238", zerolinecolor="#162238", tickfont_family="JetBrains Mono"),
    hovermode="x unified",
    hoverlabel=dict(bgcolor="#101d32", bordercolor="#285080", font_family="JetBrains Mono", font_size=11),
    legend=dict(bgcolor="rgba(0,0,0,0)", bordercolor="#162238", font_family="Outfit"),
    margin=dict(l=40, r=20, t=44, b=36),
)

# Session definitions (UTC ranges)
SESSION_MAP = {
    "London/NY ⚡": {"color": "#ffd740", "bg": "rgba(255,215,64,0.12)", "hours": (13, 16)},
    "London":       {"color": "#3d9eff", "bg": "rgba(61,158,255,0.12)",  "hours": (7,  13)},
    "New York":     {"color": "#b388ff", "bg": "rgba(179,136,255,0.12)", "hours": (16, 22)},
    "Asia/Sydney":  {"color": "#00e676", "bg": "rgba(0,230,118,0.12)",   "hours": None},
    "Off-Hours":    {"color": "#2e4560", "bg": "rgba(46,69,96,0.12)",    "hours": None},
}


# =============================================================================
# BACKEND HELPER FUNCTIONS  (all preserved exactly from original)
# =============================================================================

def fetch_json(path: str) -> dict:
    """Generic GET → dict. Returns {} on any network or parse error."""
    try:
        r = requests.get(f"{BACKEND}{path}", timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def fetch_status() -> dict:
    """Fetch backend system status from /api/system-status."""
    return fetch_json("/api/system-status")


def fetch_current(symbol: str) -> dict:
    """Fetch the current structure snapshot for a single symbol."""
    return fetch_json(f"/api/current-structure/{symbol}")


def fetch_recent_signals(symbol=None, limit: int = 100) -> pd.DataFrame:
    """Fetch recent signals, optionally filtered by symbol."""
    try:
        params = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        r = requests.get(f"{BACKEND}/api/recent-signals", params=params, timeout=3)
        r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else data.get("signals", [])
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def fetch_open_trades() -> pd.DataFrame:
    """Fetch currently open trades from /api/open-trades."""
    try:
        data = fetch_json("/api/open-trades")
        rows = data if isinstance(data, list) else data.get("trades", [])
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def fetch_trades(limit: int = 100) -> pd.DataFrame:
    """Fetch closed trade history from /api/trades."""
    try:
        r = requests.get(f"{BACKEND}/api/trades", params={"limit": limit}, timeout=3)
        r.raise_for_status()
        data = r.json()
        rows = data if isinstance(data, list) else data.get("trades", [])
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


# =============================================================================
# NEW HELPER FUNCTIONS
# =============================================================================

def fetch_lifecycle_status() -> dict:
    """Fetch active trade lifecycle data from /lifecycle/status."""
    return fetch_json("/lifecycle/status")


def check_backend_alive() -> bool:
    """Quick ping to see if backend is reachable."""
    try:
        r = requests.get(f"{BACKEND}/", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def get_trading_session() -> dict:
    """Return session config dict for current UTC hour."""
    hour = datetime.utcnow().hour
    if 13 <= hour < 16:
        return SESSION_MAP["London/NY ⚡"]
    elif 7 <= hour < 13:
        return SESSION_MAP["London"]
    elif 16 <= hour < 22:
        return SESSION_MAP["New York"]
    else:
        return SESSION_MAP["Asia/Sydney"]


def get_session_name() -> str:
    hour = datetime.utcnow().hour
    if 13 <= hour < 16:
        return "London/NY ⚡"
    elif 7 <= hour < 13:
        return "London"
    elif 16 <= hour < 22:
        return "New York"
    else:
        return "Asia/Sydney"


# =============================================================================
# PRE-FETCH (header needs these before tabs render)
# =============================================================================
backend_alive  = check_backend_alive()
utc_now        = datetime.utcnow()
session_cfg    = get_trading_session()
session_name   = get_session_name()
sess_color     = session_cfg["color"]
sess_bg        = session_cfg["bg"]

try:
    mode_resp = requests.get(f"{BACKEND}/api/system-mode", timeout=2).json()
except Exception:
    mode_resp = {"mode": "unknown"}
current_mode = mode_resp.get("mode", "unknown")

mode_badge_cls = {
    "trading": "badge-trading",
    "paused":  "badge-paused",
    "flatten": "badge-flatten",
}.get(current_mode, "badge-unknown")
mode_icons = {"trading": "▲ TRADING", "paused": "⏸ PAUSED", "flatten": "✖ FLATTEN"}
mode_label = mode_icons.get(current_mode, current_mode.upper())

conn_dot   = '<span class="pulse"></span>'     if backend_alive else '<span class="pulse pulse-red"></span>'
conn_color = "var(--green)"                    if backend_alive else "var(--red)"
conn_label = "CONNECTED"                       if backend_alive else "DISCONNECTED"

# =============================================================================
# HEADER
# =============================================================================
st.markdown(f"""
<div class="cq-header">
  <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:14px;">

    <div>
      <div class="cq-wordmark">CAPRI<span class="accent">QUANT</span></div>
      <div class="cq-subtitle">Enterprise Trading Operations Center &nbsp;·&nbsp; SMC Structure Engine v3.0</div>
    </div>

    <div style="display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
      <!-- Clock -->
      <div style="text-align:right;">
        <div style="font-family:var(--font-mono); font-size:1.05rem; color:var(--text-hi); letter-spacing:0.05em;">
          {utc_now.strftime('%H:%M:%S')}&nbsp;<span style="color:var(--text-lo); font-size:0.68rem;">UTC</span>
        </div>
        <div style="font-family:var(--font-ui); font-size:0.62rem; color:var(--text-lo);">
          {utc_now.strftime('%a %d %b %Y')}
        </div>
      </div>

      <!-- Session badge -->
      <span class="cq-session" style="background:{sess_bg}; color:{sess_color}; border:1px solid {sess_color}55;">
        {session_name}
      </span>

      <!-- System mode -->
      <span class="cq-badge {mode_badge_cls}">{mode_label}</span>

      <!-- Connection -->
      <div style="display:flex; align-items:center; gap:7px;">
        {conn_dot}
        <span style="font-family:var(--font-mono); font-size:0.62rem; color:{conn_color};">{conn_label}</span>
      </div>
    </div>

  </div>
</div>
""", unsafe_allow_html=True)

# =============================================================================
# KILL SWITCH BAR  (always visible, non-bypassable — preserved from original)
# =============================================================================
st.markdown("""
<div class="cq-ks-bar">
  <span style="font-family:var(--font-ui); font-size:0.65rem; font-weight:800;
               letter-spacing:0.12em; text-transform:uppercase; color:var(--red);">
    🛡&nbsp; SYSTEM CONTROL &nbsp;·&nbsp; NON-BYPASSABLE &nbsp;·&nbsp; AFFECTS ALL CONNECTED EAs
  </span>
  <span style="font-family:var(--font-mono); font-size:0.7rem; color:var(--text-mid);">
    POST /api/control
  </span>
</div>
""", unsafe_allow_html=True)

ks1, ks2, ks3, ks4 = st.columns([2.2, 1.5, 1.5, 0.8])

if ks1.button("🚨 EMERGENCY FLATTEN ALL + PAUSE", type="primary", use_container_width=True):
    try:
        r = requests.post(f"{BACKEND}/api/control", json={"action": "flatten_all"}, timeout=5)
        st.error(f"FLATTEN sent: {r.json()}")
        st.rerun()
    except Exception as e:
        st.error(f"Control failed: {e}")

if ks2.button("⏸️ PAUSE TRADING", use_container_width=True):
    try:
        r = requests.post(f"{BACKEND}/api/control", json={"action": "pause"}, timeout=5)
        st.warning(f"PAUSE sent: {r.json()}")
        st.rerun()
    except Exception as e:
        st.error(f"Control failed: {e}")

if ks3.button("▶️ RESUME TRADING", use_container_width=True):
    try:
        r = requests.post(f"{BACKEND}/api/control", json={"action": "resume"}, timeout=5)
        st.success(f"RESUME sent: {r.json()}")
        st.rerun()
    except Exception as e:
        st.error(f"Control failed: {e}")

if ks4.button("↺ Refresh", use_container_width=True):
    st.rerun()

# Mode-state banners
if current_mode == "flatten":
    st.error("🚨 SYSTEM IN FLATTEN MODE — ALL POSITIONS BEING CLOSED. Use RESUME to restore.")
elif current_mode == "paused":
    st.warning("⏸️ SYSTEM PAUSED — Signals suppressed. Use RESUME to restore normal trading.")

# Active alert banners
try:
    alerts_resp = requests.get(f"{BACKEND}/api/alerts", timeout=2).json()
    alerts = alerts_resp.get("alerts", [])
    if alerts:
        for a in alerts:
            lvl = a.get("level", "info").upper()
            msg = f"**{lvl}** [{a.get('type')}]: {a.get('msg')}"
            st.error(f"⚠️ {msg}") if lvl in ("ERROR", "CRITICAL") else st.warning(msg)
except Exception:
    pass

# =============================================================================
# SIDEBAR
# =============================================================================
with st.sidebar:
    st.markdown("""
    <div style="padding:8px 0 14px;">
      <span style="font-family:var(--font-mono); font-size:0.85rem; font-weight:600;
                   color:var(--accent); letter-spacing:0.12em;">CQ CONTROLS</span>
    </div>
    """, unsafe_allow_html=True)

    # Dashboard controls
    st.markdown('<div class="cq-section">Dashboard</div>', unsafe_allow_html=True)
    auto_refresh = st.checkbox("Auto-refresh (live feel)", value=True)
    refresh_sec  = st.slider("Poll interval (seconds)", 2, 30, POLL_SECONDS, step=1)

    sb_c1, sb_c2 = st.columns(2)
    with sb_c1:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()
    with sb_c2:
        if st.button("📥 Export", use_container_width=True):
            st.toast("Use the download buttons in each tab", icon="📊")

    # Risk & signal counters from live /api/system-status
    st.markdown('<div class="cq-section">Risk Manager</div>', unsafe_allow_html=True)

    status_full   = fetch_status()
    risk_state    = status_full.get("risk", {})
    metrics_snap  = status_full.get("metrics_snapshot", {})
    signals_total = metrics_snap.get("signals_total", {})
    vetoes_total  = metrics_snap.get("risk_vetoes_total", {})

    equity       = risk_state.get("equity", 0)
    daily_pnl    = risk_state.get("daily_pnl_pct", 0)
    loss_streak  = risk_state.get("loss_streak", 0)
    is_halted    = risk_state.get("is_halted", False)
    bad_ticks    = metrics_snap.get("bad_ticks_total", 0)

    daily_color = "var(--green)" if daily_pnl >= 0 else "var(--red)"
    streak_color = "var(--red)" if loss_streak >= 2 else "var(--text-hi)"
    halted_str   = "YES ⚠️" if is_halted else "NO"
    halted_color = "var(--red)" if is_halted else "var(--green)"

    st.markdown(f"""
    <div class="cq-card" style="padding:10px 12px;">
      <div class="stat-row">
        <span class="stat-lbl">Equity</span>
        <span class="stat-val">${equity:,.2f}</span>
      </div>
      <div class="stat-row">
        <span class="stat-lbl">Daily PnL</span>
        <span class="stat-val" style="color:{daily_color};">{daily_pnl:+.3f}%</span>
      </div>
      <div class="stat-row">
        <span class="stat-lbl">Loss Streak</span>
        <span class="stat-val" style="color:{streak_color};">{loss_streak}</span>
      </div>
      <div class="stat-row">
        <span class="stat-lbl">RM Halted</span>
        <span class="stat-val" style="color:{halted_color};">{halted_str}</span>
      </div>
      <div class="stat-row">
        <span class="stat-lbl">Bad Ticks</span>
        <span class="stat-val" style="color:{'var(--red)' if bad_ticks > 0 else 'var(--text-lo)'};">{bad_ticks}</span>
      </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="cq-section">Signal Engine</div>', unsafe_allow_html=True)

    total_sigs = max(sum(signals_total.values()), 1)
    buy_n  = signals_total.get("BUY", 0)
    sell_n = signals_total.get("SELL", 0)
    hold_n = signals_total.get("HOLD", 0)
    directional_pct = (buy_n + sell_n) / total_sigs * 100

    st.markdown(f"""
    <div class="cq-card" style="padding:10px 12px;">
      <div class="stat-row">
        <span class="stat-lbl">BUY</span>
        <span class="stat-val bull">{buy_n}</span>
      </div>
      <div class="stat-row">
        <span class="stat-lbl">SELL</span>
        <span class="stat-val bear">{sell_n}</span>
      </div>
      <div class="stat-row">
        <span class="stat-lbl">HOLD</span>
        <span class="stat-val dim">{hold_n}</span>
      </div>
      <div class="stat-row">
        <span class="stat-lbl">Risk Vetoes</span>
        <span class="stat-val warn">{sum(vetoes_total.values())}</span>
      </div>
      <div class="stat-row">
        <span class="stat-lbl">Directional %</span>
        <span class="stat-val">{directional_pct:.0f}%</span>
      </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")
    st.markdown(f"""
    <div style="font-family:var(--font-mono); font-size:0.58rem; color:var(--text-lo); line-height:1.8;">
      Backend: {BACKEND}<br>
      Session: {session_name}<br>
      Kill switch: always visible<br>
      Risk layers: hard + non-bypassable<br>
      Data: direct market buffer (15840 M1)
    </div>
    """, unsafe_allow_html=True)


# =============================================================================
# MAIN TABS
# =============================================================================
(
    tab_overview, tab_structure, tab_risk,
    tab_performance, tab_trades, tab_lifecycle, tab_control,
) = st.tabs([
    "📊 Live Overview",
    "🔬 Structure",
    "🛡️ Risk & Alerts",
    "📈 Performance",
    "📋 Trade Journal",
    "⚡ Lifecycle",
    "🚨 System Control",
])


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — LIVE OVERVIEW
# ═══════════════════════════════════════════════════════════════════════════════
with tab_overview:
    status  = fetch_status()
    risk    = status.get("risk", {})
    m_snap  = status.get("metrics_snapshot", {})
    tracked = status.get("symbols_tracked", [])

    # ── Top KPI Row ──────────────────────────────────────────────────────────
    kpi1, kpi2, kpi3, kpi4, kpi5, kpi6 = st.columns(6)
    kpi1.metric("Backend",       "LIVE ✅" if backend_alive else "DOWN ❌")
    kpi2.metric("M1 Buffer Cap", f"{status.get('buffer_max_m1', 15840):,}")
    kpi3.metric("Symbols",       len(tracked))
    kpi4.metric("Equity",        f"${risk.get('equity', 0):,.0f}")
    kpi5.metric("Daily PnL",     f"{risk.get('daily_pnl_pct', 0):+.2f}%")
    kpi6.metric("Last Tick",     utc_now.strftime("%H:%M:%S"))

    st.caption(
        f"Tracked: **{', '.join(tracked) if tracked else 'none yet'}** &nbsp;·&nbsp; "
        f"Buffer: 1w+4d headroom (15840 M1) &nbsp;·&nbsp; "
        f"Post-off: last 1440 M1 for trend/structure &nbsp;·&nbsp; M5 primary for decisions"
    )

    # ── Symbol Cards ─────────────────────────────────────────────────────────
    st.markdown('<div class="cq-section">Live Market Structure — Readiness per Symbol</div>', unsafe_allow_html=True)

    card_cols = st.columns(len(SYMBOLS))
    for i, sym in enumerate(SYMBOLS):
        snap = fetch_current(sym)
        with card_cols[i]:
            buf  = snap.get("buffer", {}) if isinstance(snap, dict) else {}
            m5c  = buf.get("m5_bars_in_buffer", 0)
            m5mx = buf.get("max_m5_bars", 3168)
            m5p  = buf.get("m5_pct_full", 0)

            if m5c < 3:
                readiness, r_icon, r_color = "INSUFFICIENT", "🔴", "var(--red)"
            elif m5c < 8:
                readiness, r_icon, r_color = "BASIC",        "🟡", "var(--yellow)"
            elif m5c < 20:
                readiness, r_icon, r_color = "GOOD",         "🟢", "var(--accent)"
            else:
                readiness, r_icon, r_color = "STRONG",       "✅", "var(--green)"

            insufficient = "error" in snap or snap.get("status") == "insufficient_live_data"
            bias  = "N/A" if insufficient else snap.get("bias", "?")
            price = snap.get("current_price")

            if "BULL" in bias:
                b_color = "var(--green)"
            elif "BEAR" in bias:
                b_color = "var(--red)"
            else:
                b_color = "var(--yellow)"

            st.markdown(f"""
            <div class="sym-card">
              <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                <span class="sym-label">{sym}</span>
                <span style="font-family:var(--font-mono); font-size:0.62rem; color:{r_color};">{r_icon} {readiness}</span>
              </div>
              <div class="sym-bias" style="color:{b_color};">{bias}</div>
              <div class="sym-price">{f'{price:.2f}' if price else '— —'}</div>
              <div class="sym-summary">{snap.get('structure_summary', 'Awaiting data...') if not insufficient else 'Insufficient live data'}</div>
            </div>
            """, unsafe_allow_html=True)

            if not insufficient:
                ob1, ob2, ob3 = st.columns(3)
                ob1.metric("Bull OBs", snap.get("active_bullish_obs", 0))
                ob2.metric("Bear OBs", snap.get("active_bearish_obs", 0))
                ob3.metric("Swings",   snap.get("swing_count", 0))

            bcount  = buf.get("effective_bars", buf.get("bars_in_buffer", 0))
            pct_m1  = min(buf.get("pct_full", 0) / 100, 1.0)
            pct_m5  = min(m5p / 100, 1.0)
            st.progress(pct_m1, text=f"M1: {bcount} bars ({buf.get('pct_full',0):.0f}%)")
            st.progress(pct_m5, text=f"M5: {m5c}/{m5mx} ({m5p:.0f}%)")
            st.caption("M5 ≥ 20 = full context for high-quality setups")

    # ── Signal Engine Summary ─────────────────────────────────────────────────
    st.markdown('<div class="cq-section" style="margin-top:20px;">Signal Engine Activity (cumulative since restart)</div>', unsafe_allow_html=True)

    sigs   = m_snap.get("signals_total", {})
    vetoes = m_snap.get("risk_vetoes_total", {})
    bad_t  = m_snap.get("bad_ticks_total", 0)

    se1, se2, se3, se4, se5 = st.columns(5)
    se1.metric("BUY Signals",         sigs.get("BUY",  0))
    se2.metric("SELL Signals",        sigs.get("SELL", 0))
    se3.metric("HOLD (suppressed)",   sigs.get("HOLD", 0))
    se4.metric("Risk Vetoes",         sum(vetoes.values()))
    se5.metric("Bad Ticks Rejected",  bad_t)

    if vetoes:
        items = "&nbsp; | &nbsp;".join(
            f'<span style="color:var(--red); font-family:var(--font-mono); font-size:0.7rem;">{k}: {v}</span>'
            for k, v in vetoes.items()
        )
        st.markdown(f"<div style='margin-top:4px; padding:4px 0;'>Veto reasons: {items}</div>", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2 — STRUCTURE DEEP DIVE
# ═══════════════════════════════════════════════════════════════════════════════
with tab_structure:
    st.markdown('<div class="cq-section">Structure Build-up & Signal History</div>', unsafe_allow_html=True)
    st.caption("AMD + Fib + PA + Liquidity + CRT confluence over time. The 'progress bar' toward a high-quality setup.")

    filt_c1, filt_c2 = st.columns([1, 2])
    with filt_c1:
        sig_symbol = st.selectbox("Filter by symbol", ["All"] + SYMBOLS, index=0, key="struct_sym")
    with filt_c2:
        sig_limit = st.slider("Signals to load", 30, 400, 120, step=10, key="struct_lim")

    signals_df = fetch_recent_signals(None if sig_symbol == "All" else sig_symbol, sig_limit)

    if not signals_df.empty:
        disp_cols = [
            c for c in ["ts", "symbol", "timeframe", "signal", "score", "confidence",
                        "setup", "structure_summary", "bias", "current_price", "total_confluence"]
            if c in signals_df.columns
        ]
        disp = signals_df[disp_cols].copy()
        disp["ts"] = pd.to_datetime(disp["ts"])
        st.dataframe(disp, use_container_width=True, height=320)

        if "total_confluence" in disp.columns and len(disp) > 5:
            fig = px.line(
                disp, x="ts", y="total_confluence",
                color="symbol" if "symbol" in disp.columns else None,
                title="Confluence Build-up Over Time",
                labels={"total_confluence": "Total Confluence", "ts": "Time"},
                color_discrete_sequence=["#3d9eff", "#00e676", "#ffd740", "#b388ff"],
            )
            fig.add_hline(
                y=0.48, line_dash="dash", line_color="#ff4d6a", opacity=0.6,
                annotation_text="Threshold 0.48", annotation_font_color="#ff4d6a",
            )
            fig.update_layout(**CQ_LAYOUT, height=300)
            st.plotly_chart(fig, use_container_width=True)
            st.caption("Confluence (AMD + FIB + PA + LIQ + CRT) builds before a full setup triggers a non-HOLD. Threshold ~0.48 in config.")
    else:
        st.info("No signals yet — start the EA + backend to populate history.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3 — RISK & ALERTS
# ═══════════════════════════════════════════════════════════════════════════════
with tab_risk:
    st.markdown('<div class="cq-section">Risk Circuits, Alerts & Daily Controls</div>', unsafe_allow_html=True)
    st.caption("Hard, non-bypassable risk layers (streak, daily loss) + live alerts. This is what keeps prop capital safe.")

    # Quick controls (duplicated per original intent)
    st.markdown("**Quick System Mode Controls (same as top)**")
    qc1, qc2, qc3 = st.columns(3)
    if qc1.button("🚨 EMERGENCY FLATTEN ALL", type="primary", use_container_width=True, key="risk_flat"):
        try:
            r = requests.post(f"{BACKEND}/api/control", json={"action": "flatten_all"}, timeout=5)
            st.error(str(r.json()))
        except Exception as e:
            st.error(str(e))
    if qc2.button("⏸️ PAUSE (signals → HOLD)", use_container_width=True, key="risk_pause"):
        try:
            r = requests.post(f"{BACKEND}/api/control", json={"action": "pause"}, timeout=5)
            st.warning(str(r.json()))
        except Exception as e:
            st.error(str(e))
    if qc3.button("▶️ RESUME TRADING", use_container_width=True, key="risk_resume"):
        try:
            r = requests.post(f"{BACKEND}/api/control", json={"action": "resume"}, timeout=5)
            st.success(str(r.json()))
        except Exception as e:
            st.error(str(e))

    # ── Risk Manager State (live from /api/system-status) ────────────────────
    st.markdown('<div class="cq-section">Risk Manager State (live)</div>', unsafe_allow_html=True)

    risk_full = fetch_status()
    rm  = risk_full.get("risk", {})
    qal = risk_full.get("quality_issues", {})

    rm1, rm2, rm3, rm4 = st.columns(4)
    rm1.metric("Equity",      f"${rm.get('equity', 0):,.2f}")
    rm2.metric("Daily PnL",   f"{rm.get('daily_pnl_pct', 0):+.3f}%")
    rm3.metric("Loss Streak", rm.get("loss_streak", 0))
    rm4.metric("RM Halted",   "YES ⚠️" if rm.get("is_halted") else "NO ✅")

    if rm.get("is_halted"):
        st.error("🚨 RISK MANAGER HAS HALTED TRADING — Loss streak or drawdown limit breached. Manual resume required.")

    # ── Prop Firm Challenge Tracker  (wired to real daily_pnl_pct) ───────────
    st.markdown('<div class="cq-section" style="margin-top:20px;">🏆 Prop Firm Challenge Tracker</div>', unsafe_allow_html=True)
    st.caption("Daily loss + profit target wired to live /api/system-status. Customize thresholds in code or backend config.")

    real_daily = rm.get("daily_pnl_pct", 0)
    daily_loss_pct   = abs(min(real_daily, 0))      # positive = how much of the loss limit consumed
    profit_target_pct = max(real_daily, 0)           # positive = progress toward profit target
    consistency       = max(0, min(100, 100 - rm.get("loss_streak", 0) * 10))  # streak-based proxy

    col_p1, col_p2, col_p3 = st.columns(3)
    with col_p1:
        st.metric("Daily Loss Used", f"{daily_loss_pct:.1f}%", delta=f"-{max(0, 5-daily_loss_pct):.1f}% to limit")
        st.progress(min(daily_loss_pct / 5.0, 1.0))
        st.caption("Max Daily Loss: 5% (typical FTMO-style)")
    with col_p2:
        st.metric("Profit Target", f"{profit_target_pct:.1f}%", delta=f"+{max(0, 10-profit_target_pct):.1f}% to go")
        st.progress(min(profit_target_pct / 10.0, 1.0))
        st.caption("Profit Target: 10%")
    with col_p3:
        st.metric("Consistency Score", f"{consistency}%")
        st.progress(consistency / 100.0)
        st.caption("Min 60% consistency rule (streak-based proxy)")

    if daily_loss_pct > 4.5:
        st.error("⚠️ Approaching daily loss limit — consider pausing!")
    elif profit_target_pct > 8:
        st.success("🎉 On track for profit target!")

    # ── Data Quality Panel ────────────────────────────────────────────────────
    st.markdown('<div class="cq-section" style="margin-top:20px;">Data Quality</div>', unsafe_allow_html=True)
    if qal:
        any_issues = False
        for sym, issues in qal.items():
            if issues:
                any_issues = True
                issue_str = ", ".join(issues) if isinstance(issues, list) else str(issues)
                st.warning(f"**{sym}**: {issue_str}")
        if not any_issues:
            st.success("No data quality issues detected")
    else:
        st.success("No data quality issues detected")

    # ── Alerts ───────────────────────────────────────────────────────────────
    st.markdown('<div class="cq-section" style="margin-top:20px;">Active Alerts</div>', unsafe_allow_html=True)
    try:
        alerts = fetch_json("/api/alerts").get("alerts", [])
        if alerts:
            st.error("ACTIVE ALERTS — ACTION REQUIRED")
            for a in alerts:
                st.write(f"**{a.get('level','INFO').upper()}** — {a.get('type')}: {a.get('msg')}")
        else:
            st.success("No active risk or system alerts")
    except Exception:
        st.warning("Could not fetch alerts")

    # ── Open Positions ────────────────────────────────────────────────────────
    st.markdown('<div class="cq-section" style="margin-top:20px;">Open Positions + Management Suggestions</div>', unsafe_allow_html=True)
    open_df = fetch_open_trades()
    if not open_df.empty:
        st.dataframe(open_df, use_container_width=True, height=200)
        if "management" in open_df.columns:
            for _, row in open_df.iterrows():
                if pd.notna(row.get("management")):
                    m = row["management"]
                    if isinstance(m, dict):
                        action = m.get("action", "")
                        color  = "🟢" if "BE" in action or "TRAIL" in action else "🔴"
                        st.write(f"{color} **{row.get('symbol')}** Ticket {row.get('ticket')}: {action} → new SL {m.get('new_sl')} | {m.get('reason')}")
    else:
        st.info("No open trades reported.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4 — PERFORMANCE & ATTRIBUTION
# ═══════════════════════════════════════════════════════════════════════════════
with tab_performance:
    st.markdown('<div class="cq-section">Performance Attribution & Edge Analysis</div>', unsafe_allow_html=True)
    st.caption("Understand *why* you win or lose. This separates hobby systems from prop-firm grade operations.")

    trades_df = fetch_trades(limit=300)
    if not trades_df.empty and "r_multiple" in trades_df.columns:
        r = trades_df["r_multiple"].fillna(0).astype(float)

        pf = (
            (r[r > 0].sum() / abs(r[r <= 0].sum()))
            if (r <= 0).any() and r[r <= 0].sum() != 0
            else 99.0
        )
        sharpe = f"{r.mean() / r.std():.2f}" if r.std() > 0 else "—"

        kp1, kp2, kp3, kp4, kp5 = st.columns(5)
        kp1.metric("Total Closed",   len(trades_df))
        kp2.metric("Expectancy (R)", f"{r.mean():.3f}")
        kp3.metric("Win Rate",       f"{(r > 0).mean() * 100:.1f}%")
        kp4.metric("Profit Factor",  f"{pf:.2f}")
        kp5.metric("Sharpe (est.)",  sharpe)

        # Charts — side by side
        ch_left, ch_right = st.columns(2)

        with ch_left:
            fig_hist = px.histogram(
                trades_df, x="r_multiple", nbins=25,
                title="R-Multiple Distribution",
                color_discrete_sequence=["#3d9eff"],
            )
            fig_hist.add_vline(x=0, line_color="#ff4d6a", line_dash="dash", opacity=0.55)
            fig_hist.add_vline(
                x=r.mean(), line_color="#00e676", line_dash="dot", opacity=0.75,
                annotation_text=f"Mean {r.mean():.2f}R",
                annotation_font_color="#00e676",
            )
            fig_hist.update_layout(**CQ_LAYOUT, height=300)
            st.plotly_chart(fig_hist, use_container_width=True)

        with ch_right:
            cum = r.cumsum()
            fig_cum = go.Figure()
            fig_cum.add_trace(go.Scatter(
                x=list(range(len(cum))), y=cum,
                mode="lines", name="Cumulative R",
                line=dict(color="#00e676", width=2),
                fill="tozeroy",
                fillcolor="rgba(0,230,118,0.06)",
            ))
            fig_cum.add_hline(y=0, line_color="#ff4d6a", line_dash="dash", opacity=0.4)
            layout_eq = {**CQ_LAYOUT, "title": "Equity Curve (Cumulative R)"}
            fig_cum.update_layout(**layout_eq, height=300, xaxis_title="Trade #", yaxis_title="R")
            st.plotly_chart(fig_cum, use_container_width=True)

        # Attribution tables — side by side
        at_left, at_right = st.columns(2)

        with at_left:
            if "symbol" in trades_df.columns:
                by_sym = (
                    trades_df.groupby("symbol")["r_multiple"]
                    .agg(["count", "mean", "sum"])
                    .round(3)
                )
                by_sym.columns = ["Trades", "Avg R", "Total R"]
                st.markdown('<div class="cq-section">By Symbol</div>', unsafe_allow_html=True)
                st.dataframe(by_sym.sort_values("Total R", ascending=False), use_container_width=True)

        with at_right:
            if "setup" in trades_df.columns and trades_df["setup"].notna().any():
                by_setup = (
                    trades_df[trades_df["setup"].notna()]
                    .groupby("setup")["r_multiple"]
                    .agg(["count", "mean", "sum"])
                    .round(3)
                )
                by_setup.columns = ["Trades", "Avg R", "Total R"]
                st.markdown('<div class="cq-section">By Setup</div>', unsafe_allow_html=True)
                st.dataframe(by_setup.sort_values("Total R", ascending=False), use_container_width=True)

        csv = trades_df.to_csv(index=False).encode()
        st.download_button("⬇️ Download Trade Log (CSV)", csv, "capriquant_trades.csv", "text/csv")
    else:
        st.info("No trade data with R-multiples yet for attribution analysis.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 5 — TRADE JOURNAL
# ═══════════════════════════════════════════════════════════════════════════════
with tab_trades:
    st.markdown('<div class="cq-section">Full Trade Journal + Close Reason Analysis</div>', unsafe_allow_html=True)
    st.caption("SL vs TP attribution + setup performance = the data you need to become one of the world's best prop traders.")

    trades_for_close = fetch_trades(limit=500)
    if not trades_for_close.empty:
        if "close_reason" not in trades_for_close.columns:
            trades_for_close["close_reason"] = (
                trades_for_close
                .get("notes", pd.Series([""] * len(trades_for_close)))
                .astype(str).str.lower()
                .apply(lambda x: "sl" if "sl" in x else ("tp" if "tp" in x else ""))
            )

        def make_reason_label(row):
            reason  = str(row.get("close_reason", "")).lower() if pd.notna(row.get("close_reason")) else ""
            outcome = str(row.get("outcome", "")).lower()
            if "sl" in reason or (outcome and "loss" in outcome and "sl" not in reason):
                return "🔴 SL"
            elif "tp" in reason or "take profit" in reason:
                return "🟢 TP"
            return reason[:12] if reason else ""

        trades_for_close          = trades_for_close.copy()
        trades_for_close["_reason"] = trades_for_close.apply(make_reason_label, axis=1)

        display_cols = [
            c for c in ["ts", "symbol", "direction", "entry_price", "stop_loss",
                        "close_price", "_reason", "r_multiple", "setup", "outcome"]
            if c in trades_for_close.columns
        ]
        st.dataframe(trades_for_close[display_cols].tail(150), use_container_width=True, height=320)

        sl = int(trades_for_close["_reason"].str.contains("SL", na=False).sum())
        tp = int(trades_for_close["_reason"].str.contains("TP", na=False).sum())
        jk1, jk2, jk3 = st.columns(3)
        jk1.metric("SL Hits",    sl)
        jk2.metric("TP Hits",    tp)
        jk3.metric("TP/SL Ratio", f"{tp/sl:.2f}" if sl > 0 else "∞")

        if "setup" in trades_for_close.columns:
            st.markdown('<div class="cq-section" style="margin-top:16px;">Closed Trades by Setup</div>', unsafe_allow_html=True)
            st.dataframe(
                trades_for_close[trades_for_close["setup"].notna()]
                .groupby("setup")["_reason"]
                .value_counts()
                .unstack(fill_value=0),
                use_container_width=True,
            )

        csv = trades_for_close.to_csv(index=False).encode()
        st.download_button("Download Full Journal CSV", csv, "capriquant_full_journal.csv")
    else:
        st.info("No closed trades reported yet.")


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 6 — LIFECYCLE  (new)
# ═══════════════════════════════════════════════════════════════════════════════
with tab_lifecycle:
    st.markdown('<div class="cq-section">Active Trade Lifecycle Management</div>', unsafe_allow_html=True)
    st.caption(
        "Real-time view of all trades registered with the TradeLifecycleManager. "
        "Pulled from GET /lifecycle/status. Trades appear here after /lifecycle/register is called by the EA."
    )

    lc            = fetch_lifecycle_status()
    active_trades = lc.get("active_trades", [])
    lc_count      = lc.get("count", 0)

    be_count  = sum(1 for t in active_trades if t.get("is_be"))
    avg_rr    = (
        sum(t.get("current_rr", 0) for t in active_trades) / len(active_trades)
        if active_trades else 0.0
    )

    lc1, lc2, lc3, lc4 = st.columns(4)
    lc1.metric("Active Managed Trades", lc_count)
    lc2.metric("At Break-Even",         be_count)
    lc3.metric("Avg Current R:R",       f"{avg_rr:.2f}" if active_trades else "—")
    lc4.metric("Profitable (R>0)",      sum(1 for t in active_trades if t.get("current_rr", 0) > 0))

    if active_trades:
        st.markdown('<div class="cq-section" style="margin-top:16px;">Open Managed Trades</div>', unsafe_allow_html=True)

        for t in active_trades:
            rr        = t.get("current_rr", 0)
            is_be     = t.get("is_be", False)
            direction = t.get("direction", "").upper()

            dir_color = "var(--green)" if direction == "LONG"  else "var(--red)"
            rr_color  = "var(--green)" if rr >= 1.0 else ("var(--yellow)" if rr >= 0 else "var(--red)")
            be_html   = '<span class="be-pill">BE</span>' if is_be else ""

            st.markdown(f"""
            <div class="lc-card">
              <div>
                <span class="lc-id">{t.get('trade_id', 'N/A')}</span>
                {be_html}
                <br>
                <span class="lc-meta">{t.get('symbol', '')}</span>
              </div>

              <div style="text-align:center;">
                <div class="lc-tiny">Direction</div>
                <div style="font-family:var(--font-mono); font-size:0.88rem; font-weight:700; color:{dir_color};">{direction}</div>
              </div>

              <div style="text-align:center;">
                <div class="lc-tiny">Entry</div>
                <div class="lc-meta hi">{t.get('entry', 0):.5f}</div>
              </div>

              <div style="text-align:center;">
                <div class="lc-tiny">Current R:R</div>
                <div class="lc-rr" style="color:{rr_color};">{rr:.2f}R</div>
              </div>
            </div>
            """, unsafe_allow_html=True)

        # Mini chart — R:R distribution of open trades
        if len(active_trades) >= 2:
            rr_vals = [t.get("current_rr", 0) for t in active_trades]
            ids     = [t.get("trade_id", f"T{i}") for i, t in enumerate(active_trades)]
            fig_lc = go.Figure(go.Bar(
                x=ids, y=rr_vals,
                marker_color=["#00e676" if v >= 0 else "#ff4d6a" for v in rr_vals],
                marker_line_width=0,
            ))
            fig_lc.add_hline(y=0, line_color="#6e8aaa", line_dash="dash", opacity=0.5)
            layout_lc = {**CQ_LAYOUT, "title": "Open Trade R:R Snapshot"}
            fig_lc.update_layout(**layout_lc, height=240)
            st.plotly_chart(fig_lc, use_container_width=True)
    else:
        st.info("No trades currently registered with the lifecycle manager.")
        st.caption(
            "Trades are registered via POST /lifecycle/register when the EA opens a position. "
            "They appear here automatically and track BE moves, trail stops, and current R:R."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 7 — SYSTEM CONTROL
# ═══════════════════════════════════════════════════════════════════════════════
with tab_control:
    st.markdown('<div class="cq-section">Full System Control & Health</div>', unsafe_allow_html=True)
    st.warning("These controls are non-bypassable and affect every EA connected to this backend.")

    mode_resp_ctrl = {}
    try:
        mode_resp_ctrl = requests.get(f"{BACKEND}/api/system-mode", timeout=2).json()
    except Exception:
        pass
    current_mode_ctrl = mode_resp_ctrl.get("mode", "trading")
    st.metric("Current System Mode", current_mode_ctrl.upper())

    ctrl_cols = st.columns(4)
    with ctrl_cols[0]:
        if st.button("FLATTEN ALL + PAUSE", type="primary", use_container_width=True, key="ctrl_flat"):
            requests.post(f"{BACKEND}/api/control", json={"action": "flatten_all"})
            st.rerun()
    with ctrl_cols[1]:
        if st.button("PAUSE ONLY", use_container_width=True, key="ctrl_pause"):
            requests.post(f"{BACKEND}/api/control", json={"action": "pause"})
            st.rerun()
    with ctrl_cols[2]:
        if st.button("RESUME TRADING", use_container_width=True, key="ctrl_resume"):
            requests.post(f"{BACKEND}/api/control", json={"action": "resume"})
            st.rerun()
    with ctrl_cols[3]:
        if st.button("Refresh", use_container_width=True, key="ctrl_refresh"):
            st.rerun()

    st.markdown('<div class="cq-section" style="margin-top:16px;">Live Buffer Health</div>', unsafe_allow_html=True)
    try:
        st.json(fetch_json("/debug/live-buffer"))
    except Exception:
        st.info("Buffer data unavailable")

    st.markdown('<div class="cq-section" style="margin-top:16px;">Prometheus-style Metrics</div>', unsafe_allow_html=True)
    try:
        metrics = requests.get(f"{BACKEND}/metrics", timeout=2).text
        st.code(metrics, language="text")
    except Exception:
        st.info("Metrics endpoint unavailable")


# =============================================================================
# GLOBAL AUTO REFRESH  (preserved from original)
# =============================================================================
if auto_refresh:
    time.sleep(refresh_sec)
    st.rerun()

st.caption(
    "CapriQuant Enterprise v3.0 &nbsp;·&nbsp; "
    "Leave open during session &nbsp;·&nbsp; "
    "Backend service handles 24/5 operation &nbsp;·&nbsp; "
    "All risk layers are hard and non-bypassable"
)