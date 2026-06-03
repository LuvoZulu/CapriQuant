# CapriQuant Next Development Phase: Production Hardening, Observability & Complete Trade Lifecycle

**Date:** Post P0 completion (after RiskManager hard wiring, honest backtest, test suite, secrets+CI)

**Context:** 
The 4 explicit "Recommended immediate next actions" (P0/Tier 1 core) from GAP_ANALYSIS.md are complete and verified:
- Full RiskManager as non-bypassable layer (equity/streak/daily from executed_trades, applied in /signal, realtime, backtest, now also MTF).
- Backtest uses production path + costs + RM + stats.
- Tests for structure/risk/signal/backtest (all passing).
- Secrets cleaned from index, hardened .gitignore, basic GitHub CI with smoke + regression.

Current state: Solid foundation for a high-confluence SMC system suitable for paper + cautious small-account live. Risk circuits are sacred. Data fidelity good (closed bars, timestamps). Trade close tracking (SL/TP) works in UI. But still missing key production/ops pieces for "world-class" (minimal babysitting, full lifecycle, deep observability).

**Phase Name:** Production Hardening, Observability, and Complete Trade Lifecycle (Tier 2 + selected high-ROI Tier 3)

**Overall Goals:**
- Turn the system into something you can run reliably on a Windows service with alerts, quick recovery from issues, and full visibility.
- Complete the *trade lifecycle*: great entries + risk control + intelligent post-entry adjustments driven by ongoing structure (BE, trail, scale, early exit).
- Eliminate remaining technical debt (multiple EA files causing confusion, global DB cursors, basic logging).
- Add data guards and operational controls (kill switch is #1 safety net).
- Improve analytics so you can prove/refine the edge (attribution, curves) and debug fast (structured logs, metrics).
- Maintain full backward compatibility for existing EA/UI/DB/persistence. All changes additive or with graceful fallback.
- Keep philosophy: explainable SMC (BOS/OB/FVG/liquidity/CRT/MTF/AMD), high confluence only, risk-first.

**Non-Negotiables (carry over):**
- Every signal/decision must be auditable (inputs + rationale + version).
- Risk circuits remain hard and non-bypassable.
- Backtests must stay honest (costs, full path).
- Changes should not increase fragility (pool over global, validation).

## Prioritized Task List (This Phase)

**P1 - Highest Leverage / Safety & Ops (do these first, biggest risk reduction + daily usability):**

1. **Kill Switch + Emergency Flatten + Pause Mode** (GAP Tier1 item 4)
   - Backend: New control endpoints (POST /api/control/kill-switch {action: "flatten"|"pause"|"resume"}, GET /api/system-mode).
     Global in-memory + persisted flag (simple JSON or DB "system_state" table or file).
     When active: /signal and realtime return special "FLATTEN" or "HOLD_AND_CLOSE" with notes.
   - EA (canonical): On timer/signal response, detect kill/pause. Close all positions (by magic), set internal paused state, log/report, ignore normal signals until resume. Separate timer or OnTrade for safety.
   - UI (dashboard): Prominent red "🚨 EMERGENCY: FLATTEN ALL + PAUSE TRADING" button (top, always visible). Status badge (TRADING / PAUSED / FLATTENING). "Resume Trading" button.
   - Reporting: EA reports "close" with close_reason="kill_switch" or similar so dashboard/UI tracks it.
   - Recovery: On EA/backend restart, respect persisted mode.
   - Why: You must be able to instantly stop everything if something goes wrong (news, bug, black swan). Non-negotiable for any live capital.

2. **Adopt DB Pool Everywhere + Retire Global Cursor Pattern** (GAP Tier2 #9)
   - In db.py: existing get_conn_cursor / release_conn + SimpleConnectionPool is good.
   - Refactor:
     - signals.py (all /debug, /signal paths that use cursor/conn).
     - main.py (market-data persist, report-trade, all /api/* that use global cursor).
     - Any other (e.g. inspect scripts if critical).
   - Introduce a small contextlib context manager `with_db_cursor()` for try/finally release + rollback on error.
   - Keep global conn/cursor temporarily for compatibility but mark deprecated with comments; remove from hot paths.
   - Update ensure_live_tables / persist to be pool-friendly.
   - Why: Eliminates the "one bad query aborts the entire transaction for the worker" class of bugs we fought before. Enables future multi-worker uvicorn safely.

3. **Data Quality Layer on Ingest** (GAP Tier2 #8)
   - In main.py /market-data handler + live_data.add_market_data:
     - Validate: symbol normalized, price >0 and sane (e.g. XAU 1000-4000, indices 10000-50000 range or dynamic), spread reasonable (XAU <5? configurable), timestamp not future (>now+60s) and not ancient, volume >=0, bid/ask consistent if present.
     - Monotonic per-symbol: last_ts per buffer, reject or warn on out-of-order.
     - On bad: log structured error + reason, optionally still buffer but mark "dirty", return 400 or 200+warning in response. Never let poison data reach structure compute.
   - Expose in /api/system-status and /debug/live-buffer: per-symbol "quality": {"last_bad": ts, "reasons": [...], "ok_bars": N}.
   - Add simple stats (e.g. % bad ticks rejected).
   - Why: Bad ticks from broker/EA bugs are a silent killer for structure (false BOS, bad OBs, wrong FVGs). This is cheap high-value defense.

4. **Structured Logging + Correlation IDs + /metrics Endpoint** (GAP Tier2 #6)
   - Introduce correlation: per market-data POST or /signal request, generate short request_id / signal_id (uuid4 short or timestamp+symbol).
   - Replace ad-hoc print() with proper logger (python logging + json formatter where possible, or always include keys: ts, level, component, symbol, request_id, msg, extra={buffer_bars, latency_ms, risk_veto, ...}).
   - Enhance app/utils/signal_logger.py or new structured one to include ids, full context snapshot (without huge dfs).
   - New in main.py: @app.get("/metrics") returning Prometheus text (or simple text) with counters/gauges:
     - capri_signals_total{signal, symbol}
     - capri_non_hold_rate
     - capri_structure_compute_seconds_bucket
     - capri_buffer_bars{symbol}
     - capri_risk_veto_total{reason}
     - capri_daily_r
     - capri_current_streak
     - capri_db_errors_total
     - etc.
   - Logs dir already there; rotate or keep simple.
   - UI can surface recent errors or link to logs.
   - Why: When something goes wrong at 3am (or you review a week later), you need to trace a single decision across EA->backend->DB->UI without grepping 10k lines of "HOLD".

5. **Consolidate to One Canonical EA** (GAP Tier2 #10)
   - Current variants: mt5-expert-advisor/ has 3 .mq5 + python-backend/mt5... has 1 realtime + .ex5.
   - Audit: pick the most complete (the Realtime one + FULL_PASTE_READY features: equity/balance send, validated_stop/risk_pct preference, CalculateLots with effRisk, ReportClosed with exact DEAL_REASON_SL/TP + ticket, SendTradeReport, robust ExtractJson*, OnTimer for market+reports, OnTrade for updates, HasOpenPosition, kill switch support).
   - Produce single clean file: e.g. mt5-expert-advisor/CapriQuant_Structure_EA_Realtime.mq5 (full paste ready, with header "COPY THIS ENTIRE FILE INTO MT5 EDITOR").
   - Features to ensure:
     - Always sends equity + timestamp (iTime for closed bar) + balance.
     - Prefers server risk_pct / validated_stop.
     - Reports opens + closes with close_reason="sl"|"tp"|"manual"|"kill" etc.
     - Supports kill/pause signals.
     - Magic number, symbols hardcoded or inputs for the 4.
     - Good comments, no compile warnings, version string.
   - Mark others as "legacy / do not use" or delete after copy.
   - Update any docs/bats that reference EA.
   - Why: Confusion kills reliability. One file to maintain, one to paste when redeploying.

**P2 - Trade Lifecycle & Analytics (high ROI for performance):**

6. **Post-Entry Structure-Driven Management** (GAP Tier2 #7 + Tier3 full trade mgmt)
   - Concept: Trade doesn't end at entry. Structure evolves.
   - Backend additions:
     - On /market-data (or a new background timer), for each open trade (from /api/open-trades or DB), re-compute current market_structure (M5 primary).
     - Compare to trade's entry (stored OB/swing/FVG at entry time — persist more in executed_trades on open report: entry_ob_low etc.).
     - Rules (start conservative):
       - If bullish trade and price has mitigated entry OB or filled bullish FVG + new bullish BOS/displacement → suggest "MOVE_TO_BREAKEVEN" (SL = entry + small pad).
       - If new opposing CHOCH on M5 or strong liquidity sweep against → "EARLY_EXIT" or "REDUCE".
       - If additional confluence (new OB in direction, liquidity taken + retake) → "SCALE_IN" suggestion (smaller size).
       - Trail: on new swing in direction, update TP or move SL to recent swing low (breathing).
     - New response fields or dedicated lightweight endpoint /api/management?symbol=... returning list of {ticket, action: "MOVE_BE"|"TRAIL_SL"|"EXIT"|"SCALE", new_sl?, new_tp?, rationale, confidence}.
     - Or augment normal signal responses when there are opens.
   - EA side:
     - Track open positions (already does via magic).
     - On timer or trade events, GET management (or parse from signal if present).
     - Apply: OrderModify for SL/TP changes, OrderClose for exits. Report the management close with reason.
     - Only act on high confidence; log why.
   - Persistence: when opening, store more context (entry_structure json snippet).
   - UI: show "management actions taken" or pending suggestions in open trades section.
   - Backtest support: extend replay to simulate management rules and measure improved R.
   - Why: This is the difference between "occasional good R2 trades" and "consistent edge with controlled drawdowns". Structure is dynamic — use it after entry too.

7. **Dashboard: Equity Curve, Attribution, Kill Controls, Risk Visibility** (Tier3)
   - Using existing /api/trades + new /api/system-status + risk queries:
     - Equity curve: cumulative realized (from r_multiple * assumed risk or store realized_pnl on close reports; plotly line with drawdown bands).
     - Attribution tables/cards: 
       - By setup (OB_RETRACEMENT, LIQUIDITY_SWEEP, CRT_..., etc.): winrate, avg R, count, total R.
       - By close_reason (SL vs TP): % SL, avg loss R on SL, avg win R on TP.
       - By session (AMD phase).
       - By symbol.
     - Current risk status: streak, today_r, effective risk_pct (from last signal or query), goal progress (if using RiskParams).
     - Integrate kill/pause buttons + big status.
     - Signal history: clickable rows that show full rationale + key market_structure snapshot (if stored).
   - Keep all previous UI (structure cards, buildup chart, open/closed SL/TP) — append/enhance only.
   - Why: You can't improve what you can't see. Attribution tells you which setups/times are working.

8. **Restore + Fully Integrate CRT** (Tier3)
   - crt.py was created early but source is currently absent (only pycache remnant).
   - Re-implement pure analyzer (from prior context: displacement range, OB proximity, manip/expansion timing, liq+range confluence, bias credit → signed score at structural spots).
   - Wire:
     - In confluence.py evaluate_setups: compute crt_score = analyze_crt... (ms, recent_displacement), add to total_confluence (weight ~0.25-0.3), append "CRT_RANGE" to confluences when strong.
     - Include in get_structure_signal contextual scores.
     - MarketStructure.to_dict() expose crt levels if any.
     - Backtest: it will flow through get_structure_signal.
     - UI: show CRT in confluences and setup names.
   - Add basic test in test_signal_path or new.
   - Why: Explicit user request early ("integrate fully working CRT to the current system also"). Adds another independent confluence axis for higher quality / different setups.

**P3 - Polish & Future-Proof (lower priority this phase, do if time):**

- Central config: simple class or pydantic (add dep?) for risk params (max risk, daily cap, goal), min_confluence, symbols list, costs, kill behavior, etc. Load from env + override. Per-symbol tweaks.
- Live-vs-backtest parity script: given recent market_data for a symbol/window, run replay with same params, compare produced signals (signal, setup, stops) vs what was actually emitted/logged at the time. Alert on divergence.
- Docker basics: Dockerfile for backend (uvicorn), docker-compose with postgres (for non-Windows dev/test), healthchecks. Keep Windows service as primary.
- Session/AMD improvements: realized vol profile instead of pure clock.
- More immutable audit: on every decision, store richer snapshot in live_signals (full ms dict already partially there).
- Notebook templates for research.

**Execution Rules for this Phase:**
- Always read relevant file(s) before editing.
- Use todo_write (merge=true for updates) to track: mark in_progress only one at a time, complete immediately when done + verified (import, test run, manual smoke).
- Prefer small targeted search_replace over big rewrites.
- Add minimal tests/smokes for new logic (especially kill, data quality, management).
- Update GAP_ANALYSIS.md with progress section at top.
- Create/update this NEXT_PHASE.md with status.
- For EA changes: always produce a full clean paste-ready file + clear "what changed" notes.
- Verify end-to-end where possible: e.g. for kill — simulate via curl or python requests + check response; for pool — check no direct global in hot paths after.
- After major item: run the full direct test suite we have.
- Git: commit at end of logical groups (e.g. after kill switch + UI).
- No breaking changes to existing flows without fallback.
- Since user is on Windows/PowerShell: keep all .bat, service scripts working.

**Success Metrics (when phase feels "done enough to pause"):**
- Kill switch: UI button triggers, EA would flatten (verified in code + simulated), persisted, resume works. Dashboard shows mode.
- Pool: <5 direct "from app.db import conn" or "conn.cursor()" in api/main paths (only in controlled places).
- Data quality: bad tick (e.g. spread=999, price=0, future ts) is rejected/logged with reason, does not produce signal.
- Logging: key paths (market-data, signal gen, report, risk veto) emit structured records with ids; /metrics returns useful text.
- One EA: only 1 recommended .mq5 in repo root mt5 dir, others clearly legacy or removed. It supports kill + reports management closes.
- Post-entry: at least BE + one trail/early exit rule implemented + exercised in backtest + EA can apply simple SL move.
- Dashboard: equity curve renders (even if 0 trades), attribution tables populated from real closed trades, kill buttons present, risk streak visible.
- CRT: contributes non-zero to some scores in synthetic tests + real data if available.
- All existing + new tests pass.
- No regression in current live signal quality or UI (preserve cards, history, SL/TP badges).
- Clean git (relevant files only).

**Dependencies / Order Notes:**
- Kill switch can start early (doesn't depend on much).
- Pool refactor should be early (affects stability of everything else).
- Data quality before heavy post-entry (cleaner data = better management decisions).
- EA clean can be parallel with backend changes (provide updated EA that supports new signals).
- Post-entry + dashboard after core safety items.
- CRT can be slotted in when confluence touched.

**How to Track Progress:**
This file + todo list (via agent tool) + updates to GAP_ANALYSIS.md + git commits with "phase2: ..." messages.

**After This Phase:**
Re-assess with user: next could be advanced research (parity harness, notebooks), infra (Docker prod deploy, alerts via email/telegram), or more strategies.

**User Note:** This is ambitious but broken into small verifiable steps. We will outline (this doc), then execute top-down using tools, marking todos live. We stop or adjust based on verification feedback. All changes will be explained with file:line refs where relevant.

## Execution Status (updated live during phase)

**Completed in this session (P1 + partial P2):**
- 1. Kill switch fully: backend /api/system-mode + /api/control (flatten/pause/resume + persist), mode override in all signal paths (realtime + /signal + early holds), UI prominent controls + warnings at top of dashboard (preserves prior UI), EA CloseAll + detection in ProcessSignalResponse (both main variants updated). Verified logic.
- 2. DB pool adoption: added db_cursor() context manager, refactored main.py (persist, open-trades, recent-signals, trades), signals.py (debug counts, fetch, signal count query). Hot paths now prefer pool + safe rollback.
- 3. Data quality: ingest gate in /market-data (price/spread/ts/volume/monotonic sanity + symbol bounds), records bad for /status, skips realtime structure on bad, live_data comment, quality in system-status.
- 4. Structured + metrics: req_id on data, /metrics prometheus text (mode, buffers, quality counts, up), logger ids in key paths.
- 5. EA canonical: updated FULL_PASTE_READY header as recommended, added CloseAll + kill handling to it (and realtime variant already had). Kill now in both for safety during transition.

**Remaining (will continue if requested or in follow-up):**
- Post-entry mgmt, full dashboard curve/attribution (P2-6/7), CRT restore+wire (P2-8).

All changes preserve previous functionality + append safety/ops features.
