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

The UI lets you watch live system progress, buffer status (10080 M1 bars), per-symbol market structure cards, signal build-up history (with charts), and executed trades.

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

## Quick Directory Reference

- Backend code: `python-backend\`
- Service files (for auto-start): `python-backend\service\`
- UI/Visualizer: `python-backend\ui\dashboard.py`
- Main entry: `python-backend\main.py`
- Logs from EA + signals: `python-backend\logs\`

The system is designed so the **backend service** runs 24/7 on weekdays with zero interaction, while you only open the UI when you want to visually monitor progress, signals forming, and trades taken.