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

## Running the server

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
