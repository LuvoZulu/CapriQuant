# CapriQuant — Structure-First Quant Trading System

**Honest Assessment (May 2026)**

The original feature builder + 6 strategy modules (AMD / Fibonacci / Price Action / etc.) were **not effective** for the stated goal of reliable automatic trading using real market structure, AMD concepts, Fibonacci at structure, and price action.

They were ~90% classic lagging indicators (EMA crosses, vanilla RSI, MACD) with superficial "smart money" naming. Swing detection was broken (fixed rolling window), session logic was pure clock time, and the consensus was an arbitrary weighted average.

**We have begun a full replacement** focused on:

- Proper swing / pivot detection (left+right confirmation)
- Market structure (BOS / CHOCH)
- Real Order Blocks, Liquidity pools, Fair Value Gaps
- Contextual AMD (actual session range behavior + manipulation detection)
- Fibonacci **only** at structural confluence
- A confluence engine with explicit vetoes instead of score averaging
- Dynamic risk management matched to aggressive goals (R200 → R17k/3 weeks)

## Current Status

- New `structure.py` engine is live and produces rich `MarketStructure`
- New `/signal/{symbol}/{timeframe}?engine=structure` returns explainable, high-quality setups
- Risk manager + modern MQL5 EA included
- Legacy engine still works behind `?engine=legacy`

**Strong recommendation:** Paper trade the new `?engine=structure` version extensively before using real money. The old system had no validated edge.

## Running the server (as a Windows Service)

The backend is designed to run as a Windows Service (see "Running the Backend as a Windows Service" section below).

It listens on `http://127.0.0.1:8001` (or 0.0.0.0:8001 inside the service).

For local development you can still run manually:

```bash
cd python-backend
uvicorn main:app --reload --port 8001
```

## Getting a modern structural signal

```http
GET /signal/XAUUSD/M5?engine=structure
GET /signal/US30/M15?engine=structure
```

The response now includes:
- `setup`, `confluences[]`, `rationale`
- `stop_suggestion`, `tp1`, `tp2` (structural)
- Full `market_structure` breakdown (active OBs, unfilled FVGs, liquidity, session phase, recent BOS/CHOCH, etc.)

## MT5 EA

Use the improved EA in `mt5-expert-advisor/CapriQuant_Structure_EA.mq5`.

It:
- Sends rich market data
- Polls the structure engine
- Only trades high-confluence setups with proper structural stops
- Respects risk limits

## Next Priorities & Status (Current)

**COMPLETED in this pass:**
- Full rewrite of contextual AMD, Fibonacci (confluence only), Price Action, and Liquidity strategies
- Strong integration inside the confluence engine
- Complete production MQL5 EA (paste-ready file in `mt5-expert-advisor/`)
- Dynamic risk manager tuned for your R200 → R17k goal
- Signal logging (auto)
- Backtesting replay harness skeleton ready

**IT IS NOW TIME TO BACKTEST**

You currently have no historical data. The moment you export 6–12+ months of M5/M15 data for XAUUSD + NAS100 + US30, run:

```python
from app.backtest.replay import run_backtest
import pandas as pd

df = pd.read_csv("your_xauusd_m5_data.csv")   # must have timestamp,open,high,low,close,volume
results = run_backtest(df, symbol="XAUUSD", timeframe="M5")
print(results["summary"])
```

This will tell you very quickly if the new structure engine has positive expectancy.

## Recommended Immediate Next Actions

1. Export historical data from MT5 (at least 8-12 months on M5 and M15 for your symbols)
2. Run the backtest harness on it
3. Review the trade log + r-multiples
4. Only after positive expectancy on walk-forward → increase live aggression carefully

The new system is now dramatically better than the original indicator soup. Whether it is *good enough* for your extremely aggressive goal is something only real backtesting + forward testing will prove.

---

If you want to go back to the old (now deprecated) behavior for any reason, call with `?engine=legacy`.

---

## Running the Backend as a Windows Service (Auto-start, No Manual Launch)

The goal is for the backend (FastAPI + realtime structure engine) to run automatically when you turn on your PC, with **no manual starting**, and only on weekdays.

### Recommended Method: NSSM (Simplest & Most Reliable)

1. **Download NSSM**
   - Go to https://nssm.cc/download
   - Download the latest version and extract `nssm.exe` (use the 64-bit version if your Windows is 64-bit).
   - Put it in an easy location, e.g. `C:\Tools\nssm\nssm.exe`

2. **Open Command Prompt as Administrator**

3. **Install the Service** (easiest: use the new installer helper as Administrator):

   Double-click or run as Admin:
   ```
   python-backend\service\install_as_service.bat
   ```

   Or manually with the wrapper:
   ```cmd
   C:\Tools\nssm\nssm.exe install CapriQuantBackend "C:\Users\Kaos\Documents\2026\Programming\CapriQuant\python-backend\service\start_capriquant.bat"
   ```

4. **Configure the Service** (run these commands):

   ```cmd
   C:\Tools\nssm\nssm.exe set CapriQuantBackend AppDirectory "C:\Users\Kaos\Documents\2026\Programming\CapriQuant\python-backend"
   C:\Tools\nssm\nssm.exe set CapriQuantBackend Start SERVICE_AUTO_START
   C:\Tools\nssm\nssm.exe set CapriQuantBackend AppStdout "C:\Users\Kaos\Documents\2026\Programming\CapriQuant\python-backend\logs\service_stdout.log"
   C:\Tools\nssm\nssm.exe set CapriQuantBackend AppStderr "C:\Users\Kaos\Documents\2026\Programming\CapriQuant\python-backend\logs\service_stderr.log"
   ```

   (Create the `logs` folder first if it doesn't exist.)

5. **Start the Service**

   ```cmd
   C:\Tools\nssm\nssm.exe start CapriQuantBackend
   ```

   Or open `services.msc`, find "CapriQuantBackend", and start it.

6. **Verify**
   - The backend should now be running at `http://127.0.0.1:8001`
   - Your MT5 EA should be able to POST market data and receive realtime signals without you starting anything.
   - On weekends the service will sleep (logic is inside `service\run_as_windows_service.py`).

**Important Notes:**
- The weekday-only logic lives in `python-backend/service/run_as_windows_service.py`. On Sat/Sun it sleeps instead of running the server.
- Make sure the Python interpreter used by the service has all dependencies installed (`fastapi`, `uvicorn`, `psycopg2`, `pandas`, etc.).
- You can manage the service with `nssm` commands or `services.msc`.
- To remove the service later: `nssm remove CapriQuantBackend confirm`

### How to Stop the Service

To stop the running CapriQuantBackend service:

**Easiest (recommended):**

Double-click or run as Administrator:
```
python-backend\service\stop_service.bat
```

**Manual methods (as Administrator in Command Prompt):**

Using NSSM (if installed at the usual location):
```cmd
C:\Tools\nssm\nssm.exe stop CapriQuantBackend
```

Using Windows built-in:
```cmd
sc stop CapriQuantBackend
```

**Using the GUI (no admin prompt needed for this step):**
1. Press `Win + R`, type `services.msc` and press Enter.
2. Scroll to find **CapriQuantBackend**.
3. Right-click it → **Stop**.

After stopping, the backend will no longer respond on port 8001. Your MT5 EA will stop receiving signals until you start the service again.

To check status:
```cmd
C:\Tools\nssm\nssm.exe status CapriQuantBackend
```
or
```cmd
sc query CapriQuantBackend
```

Alternative (pure Python service without NSSM) is commented inside `run_as_windows_service.py` (requires `pip install pywin32` and registration commands).

---

## Running the UI / Visualizer (Streamlit Dashboard)

The UI lets you watch live system progress, buffer status (1440 M1 bars = strict 1 day, direct from live market not DB), per-symbol market structure cards, signal build-up history (with charts), and executed trades.

### Steps

1. Open a normal Command Prompt (no admin needed).

2. Install the UI dependencies (only needed once):

   ```bash
   cd C:\Users\Kaos\Documents\2026\Programming\CapriQuant\python-backend
   pip install streamlit pandas requests
   ```

3. Run the dashboard (UI can run on any port, default is 8501):

   ```bash
   streamlit run ui/dashboard.py --server.address 127.0.0.1
   ```

   Or simply double-click the helper (recommended):

   ```
   python-backend\ui\run_ui.bat
   ```

4. It will open in your browser (e.g. http://127.0.0.1:8501). The dashboard fetches all data from the backend on 8001.

**Pro tip for convenience:** We have already created `python-backend\ui\run_ui.bat` that runs the UI on the default Streamlit port while correctly connecting to the backend on 8001.

You can also create a desktop shortcut pointing to:

```bat
@echo off
cd /d "C:\Users\Kaos\Documents\2026\Programming\CapriQuant\python-backend"
python -m streamlit run ui\dashboard.py --server.address 127.0.0.1
```

Name it "CapriQuant Visualizer". Double-click whenever you want to open the dashboard. The backend service runs independently.

**Usage Tips:**
- The backend **Windows service must be running** for the UI to show live data.
- You can leave the tab open all day — it auto-refreshes.
- Use the sidebar to control auto-refresh speed and filter by symbol.
- Close the tab / stop the Streamlit process when you don't want it (the backend service keeps running independently).

This UI is completely optional. The MT5 EA communicates directly with the backend service.

---

## Recent Major Additions (Phase 2 + Recommendations)

- **Post-entry management**: Automatic BE on OB/FVG mitigation, trailing to swings, early exit on opposing CHOCH. Fully E2E (backend computes from live structure, EA applies, dashboard shows suggestions). Respects kill/pause.
- **CRT fully integrated**: Range confluence analyzer contributing to scores and setup names.
- **MTF as default**: `engine=structure` (and realtime) now prefers MTF precision path (M5 primary) with graceful single-TF fallback.
- **Central config**: `app/config.py` + env vars (CAPRI_*) for risk, thresholds, symbols, etc. No more magic numbers in key places.
- **Kill switch + alerts**: Full E2E flatten/pause/resume (UI big red buttons, API, EA honors). Basic alerts for mode, streak, daily loss, data quality (/api/alerts + UI banner).
- **Observability**: /metrics (prometheus text), data quality gate, structured logs with ids.
- **Dashboard**: Equity curve (cum R), attribution by symbol/setup/close_reason, management actions visible, kill controls.
- **EA**: One canonical (FULL_PASTE_READY recommended), setup reporting, management + kill support.
- **Dev tools**: Docker (compose with postgres), parity checker script (check_parity.py), DB pool everywhere, tests for core + new features.
- **Honest everything**: Backtest with costs + RM, live-vs-backtest parity tool.

**To enable new features**: Use the updated .env.example. MTF is now default. Management on by default via config.

Paper trade thoroughly. Use the kill switch liberally while building confidence.

See GAP_ANALYSIS.md and NEXT_RECOMMENDATIONS.md for full history and future plan.

---

## Monitoring Metrics

To monitor the health, buffers, risk state, alerts, and overall system state, poll these backend endpoints (default base URL: `http://127.0.0.1:8001`).

### Primary Endpoint for Metrics
- `GET /metrics`  
  Returns **Prometheus-compatible text metrics**. Best for Grafana, Prometheus, or custom scrapers.  
  Current metrics include:
  - `capri_system_mode{mode="trading|paused|flatten"} 1`
  - `capri_buffer_bars{symbol="XAUUSD"} N`
  - `capri_buffer_m5_bars{symbol="XAUUSD"} N`
  - `capri_data_quality_bad_count{symbol="XAUUSD"} N`
  - `capri_up 1`

### Recommended Endpoints for Full Monitoring
Poll these regularly (every 5–30 seconds recommended):

| Endpoint                        | Purpose                                              | Format |
|---------------------------------|------------------------------------------------------|--------|
| `/api/system-status`            | Full status (mode, buffers, quality issues, **alerts**) | JSON |
| `/api/alerts`                   | Current active alerts (kill switch, streak, daily loss, data quality) | JSON |
| `/api/health`                   | Quick health check + mode + buffer OK flag          | JSON |
| `/debug/live-buffer`            | Buffer status for all tracked symbols               | JSON |
| `/debug/live-buffer/{symbol}`   | Detailed buffer info for one symbol                 | JSON |
| `/api/system-mode`              | Current kill/pause/flatten mode                     | JSON |
| `/api/open-trades`              | Live open positions + management suggestions (BE/trail/close) | JSON |

### Additional Useful Endpoints
- `/api/recent-signals` — Recent signals (with confluence, setup, rationale)
- `/api/trades` — Historical executed trades (r_multiple, close_reason, setup)
- `/signal/{symbol}/{timeframe}?engine=structure` — On-demand current signal

**Simple Python monitoring loop example**:

```python
import requests
import time

BASE = "http://127.0.0.1:8001"

while True:
    try:
        metrics_text = requests.get(f"{BASE}/metrics", timeout=3).text
        status = requests.get(f"{BASE}/api/system-status", timeout=3).json()
        alerts = requests.get(f"{BASE}/api/alerts", timeout=3).json()

        print("=== Metrics (first 300 chars) ===")
        print(metrics_text[:300])
        print("Alerts:", alerts.get("alerts", []))
        # Add your own logic here: log, email, Slack webhook, etc.
    except Exception as e:
        print("Monitor error:", e)
    time.sleep(15)
```

The Streamlit dashboard (`python-backend/ui/dashboard.py`) already polls several of these endpoints (`/api/system-status`, `/debug/live-buffer`, `/api/alerts`, `/api/open-trades`).

For production monitoring:
- Scrape `/metrics` with Prometheus/Grafana.
- Use the JSON endpoints for custom dashboards or alerting scripts.
- Consider exposing them only on localhost or via a reverse proxy for security.

---

## Quick Directory Reference

- Backend code: `python-backend\`
- Service files (for auto-start): `python-backend\service\`
- UI/Visualizer: `python-backend\ui\dashboard.py`
- Main entry: `python-backend\main.py`
- Logs from EA + signals: `python-backend\logs\`

The system is designed so the **backend service** runs 24/7 on weekdays with zero interaction, while you only open the UI when you want to visually monitor progress, signals forming, and trades taken.