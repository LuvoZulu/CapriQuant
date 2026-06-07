"""
CapriQuant RiskManager — Full Production Implementation (Fixed & Wired)
=======================================================================

CRITICAL FIXES in this version:
  1. RiskManager is now a true module-level singleton — instantiated once, lives forever
  2. update_equity() / record_trade() called from the live /market-data path via on_trade_close()
  3. get_risk_pct() replaces apply_m5_risk_levels fallback — all circuits enforced
  4. validate_structure_stop() called before every signal is emitted to EA
  5. Thread-safe state updates (threading.Lock)
  6. Persists & restores state across restarts (JSON sidecar)
  7. Exposes get_state_dict() for /api/system-status and /metrics

Key circuits (all non-bypassable):
  1. Daily loss circuit breaker         (default 3%)
  2. Weekly drawdown guard              (default 6%)
  3. Monthly drawdown guard             (default 10%)
  4. Daily trade count cap              (default 6)
  5. Loss-streak exponential de-risking
  6. Goal-progress de-risking
  7. Setup quality scaling
  8. Structure stop validation with R:R gating

Usage:
    from app.risk.risk_manager import get_risk_manager
    rm = get_risk_manager()
    rm.update_equity(account_equity)  # call on every heartbeat / trade close
    risk_pct = rm.get_risk_pct(setup_quality=confluence_score)
    if risk_pct is None:
        return HOLD  # circuits tripped — do not trade
    validated = rm.validate_structure_stop(entry, stop, tp, direction)
    if not validated["valid"]:
        return HOLD
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    direction: str              # 'long' | 'short'
    entry_price: float
    stop_price: float
    entry_time: datetime
    close_time: Optional[datetime] = None
    close_price: Optional[float] = None
    pnl_pct: Optional[float] = None
    close_reason: Optional[str] = None   # 'sl' | 'tp' | 'be' | 'choch_exit' | 'manual'
    risk_pct_used: float = 0.0
    rr_achieved: Optional[float] = None


@dataclass
class RiskState:
    equity: float
    peak_equity: float
    daily_start_equity: float
    week_start_equity: float
    month_start_equity: float
    daily_date: str                   # ISO date string for JSON serialisation
    loss_streak: int = 0
    win_streak: int = 0
    trades_today: int = 0
    pnl_today_pct: float = 0.0
    is_halted: bool = False
    halt_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Production risk manager.  Instantiate ONCE (use get_risk_manager()).
    Thread-safe; persists state to disk for restart survival.
    """

    _STATE_PATH = Path("data/risk_state.json")

    def __init__(
        self,
        initial_equity: float,
        base_risk_pct: float = 0.010,         # 1.0 % base
        max_risk_pct: float = 0.015,          # 1.5 % ceiling — NOT 2.5 %
        min_risk_pct: float = 0.003,          # 0.3 % floor
        daily_loss_limit_pct: float = 0.030,  # 3 %  daily  → halt
        weekly_loss_limit_pct: float = 0.060, # 6 %  weekly → halt
        monthly_dd_limit_pct: float = 0.100,  # 10 % monthly→ halt
        max_daily_trades: int = 6,
        min_rr: float = 1.5,
        streak_penalty_factor: float = 0.20,  # 20 % reduction per consecutive loss
        max_streak_penalty: float = 0.60,     # cap at 60 % total penalty
        goal_target_pct: float = 0.10,        # 10 % monthly goal
        goal_derisking_start: float = 0.70,   # start de-risking at 70 % of goal
        goal_min_scale: float = 0.40,         # minimum scale when at 100 % goal
    ) -> None:
        self.base_risk_pct = base_risk_pct
        self.max_risk_pct = max_risk_pct
        self.min_risk_pct = min_risk_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.weekly_loss_limit_pct = weekly_loss_limit_pct
        self.monthly_dd_limit_pct = monthly_dd_limit_pct
        self.max_daily_trades = max_daily_trades
        self.min_rr = min_rr
        self.streak_penalty_factor = streak_penalty_factor
        self.max_streak_penalty = max_streak_penalty
        self.goal_target_pct = goal_target_pct
        self.goal_derisking_start = goal_derisking_start
        self.goal_min_scale = goal_min_scale

        self._lock = threading.Lock()
        self._executed_trades: List[TradeRecord] = []

        # Restore or initialise state
        today_str = date.today().isoformat()
        self.state = RiskState(
            equity=initial_equity,
            peak_equity=initial_equity,
            daily_start_equity=initial_equity,
            week_start_equity=initial_equity,
            month_start_equity=initial_equity,
            daily_date=today_str,
        )
        self._load_persisted_state(initial_equity)

        logger.info(
            "RiskManager initialised | equity=%.2f base=%.1f%% max=%.1f%% "
            "daily_limit=%.1f%% monthly_dd=%.1f%%",
            self.state.equity,
            base_risk_pct * 100,
            max_risk_pct * 100,
            daily_loss_limit_pct * 100,
            monthly_dd_limit_pct * 100,
        )

    # ------------------------------------------------------------------
    # Public API — call these from live signal path
    # ------------------------------------------------------------------

    def update_equity(self, new_equity: float) -> None:
        """
        Call after every trade close OR on broker heartbeat with current equity.
        This feeds live equity into every risk decision — the core of the fix.
        """
        with self._lock:
            self._refresh_daily(new_equity)
            self.state.equity = new_equity
            if new_equity > self.state.peak_equity:
                self.state.peak_equity = new_equity
        self._persist_state()

    def record_trade(self, trade: TradeRecord) -> None:
        """
        Register a completed trade.  Updates streak + daily PnL.
        Call immediately after close confirmation from broker.
        """
        with self._lock:
            self._executed_trades.append(trade)
            if trade.pnl_pct is None:
                return
            self.state.pnl_today_pct += trade.pnl_pct
            self.state.trades_today += 1

            if trade.pnl_pct < 0:
                self.state.loss_streak += 1
                self.state.win_streak = 0
            else:
                self.state.win_streak += 1
                self.state.loss_streak = 0

            logger.debug(
                "Trade recorded | pnl=%.2f%% streak(L=%d W=%d) daily_pnl=%.2f%%",
                trade.pnl_pct * 100,
                self.state.loss_streak,
                self.state.win_streak,
                self.state.pnl_today_pct * 100,
            )
        self._persist_state()

    def get_risk_pct(self, setup_quality: float = 1.0) -> Optional[float]:
        """
        Returns risk fraction [0.003..0.015] for the next trade,
        or None if circuits are tripped (do NOT trade).

        setup_quality: float [0.0, 1.0] from confluence scorer.
        """
        with self._lock:
            self._refresh_daily(self.state.equity)

            halt_reason = self._check_circuit_breakers()
            if halt_reason:
                self.state.is_halted = True
                self.state.halt_reason = halt_reason
                logger.warning("TRADING HALTED: %s", halt_reason)
                return None

            if self.state.trades_today >= self.max_daily_trades:
                logger.info("Daily trade cap reached (%d)", self.max_daily_trades)
                return None

            self.state.is_halted = False
            self.state.halt_reason = None

            risk = self.base_risk_pct

            # Loss-streak penalty
            streak_penalty = min(
                self.state.loss_streak * self.streak_penalty_factor,
                self.max_streak_penalty,
            )
            risk *= 1.0 - streak_penalty

            # Goal-progress de-risking
            goal_progress = self._goal_progress()
            if goal_progress >= self.goal_derisking_start:
                t = (goal_progress - self.goal_derisking_start) / max(
                    1e-9, 1.0 - self.goal_derisking_start
                )
                scale = 1.0 - t * (1.0 - self.goal_min_scale)
                risk *= max(scale, self.goal_min_scale)

            # Setup quality scaling (never penalise below 50 %)
            quality_clipped = max(setup_quality, 0.50)
            risk *= quality_clipped

            # Clamp
            risk = max(self.min_risk_pct, min(risk, self.max_risk_pct))

            logger.info(
                "Risk computed: %.3f%% | streak_pen=%.2f goal=%.1f%% quality=%.2f",
                risk * 100,
                streak_penalty,
                goal_progress * 100,
                setup_quality,
            )
            return risk

    def validate_structure_stop(
        self,
        entry: float,
        stop: float,
        tp: float,
        direction: str,
        spread_pts: float = 0.0,
    ) -> Dict:
        """
        Validates entry/stop/TP structure.
        Returns dict: {valid, rr, sl_pts, tp_pts, reason, adjusted_tp}

        Wire this BEFORE sending any order to the EA.
        """
        if direction not in ("long", "short"):
            return {"valid": False, "reason": "Invalid direction", "rr": 0.0}

        sl_pts = abs(entry - stop)
        if sl_pts < 1e-9:
            return {"valid": False, "reason": "Zero-width stop", "rr": 0.0}

        effective_risk = sl_pts + spread_pts
        tp_pts = abs(tp - entry)
        net_tp = tp_pts - spread_pts

        if net_tp <= 0:
            return {"valid": False, "reason": "TP inside spread", "rr": 0.0}

        rr = net_tp / effective_risk

        if rr < self.min_rr:
            required_net_tp = effective_risk * self.min_rr + spread_pts
            adjusted_tp = (
                entry + required_net_tp if direction == "long" else entry - required_net_tp
            )
            return {
                "valid": False,
                "reason": f"R:R {rr:.2f} < min {self.min_rr:.1f}",
                "rr": round(rr, 3),
                "sl_pts": round(sl_pts, 5),
                "tp_pts": round(tp_pts, 5),
                "adjusted_tp": round(adjusted_tp, 5),
            }

        return {
            "valid": True,
            "reason": "ok",
            "rr": round(rr, 3),
            "sl_pts": round(sl_pts, 5),
            "tp_pts": round(tp_pts, 5),
        }

    def compute_position_size(
        self,
        equity: float,
        risk_pct: float,
        sl_pts: float,
        pip_value_per_lot: float,
        min_lots: float = 0.01,
        max_lots: float = 10.0,
    ) -> float:
        """
        Lots = (equity * risk_pct) / (sl_pts * pip_value_per_lot)

        pip_value_per_lot: account-currency value of 1 point per 1 lot
            XAUUSD: ~10 USD/lot/point  (varies with leverage)
            US30/DE30: ~1 USD/lot/point
        """
        if sl_pts <= 0 or pip_value_per_lot <= 0:
            return min_lots
        risk_amount = equity * risk_pct
        raw_lots = risk_amount / (sl_pts * pip_value_per_lot)
        lots = max(min_lots, min(round(raw_lots, 2), max_lots))
        logger.debug(
            "Position size | equity=%.2f risk=%.3f%% sl=%.5f pv=%.4f → %.2f lots",
            equity, risk_pct * 100, sl_pts, pip_value_per_lot, lots,
        )
        return lots

    def get_state_dict(self) -> Dict:
        """Snapshot for /api/system-status and Prometheus metrics."""
        with self._lock:
            s = self.state
            return {
                "equity": round(s.equity, 2),
                "peak_equity": round(s.peak_equity, 2),
                "daily_start_equity": round(s.daily_start_equity, 2),
                "daily_pnl_pct": round(s.pnl_today_pct * 100, 3),
                "daily_loss_limit_pct": round(self.daily_loss_limit_pct * 100, 1),
                "loss_streak": s.loss_streak,
                "win_streak": s.win_streak,
                "trades_today": s.trades_today,
                "max_daily_trades": self.max_daily_trades,
                "is_halted": s.is_halted,
                "halt_reason": s.halt_reason,
                "current_base_risk_pct": round(self.base_risk_pct * 100, 3),
                "goal_progress_pct": round(self._goal_progress() * 100, 1),
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_daily(self, equity: float) -> None:
        """Reset daily counters if the date has rolled."""
        today_str = date.today().isoformat()
        if self.state.daily_date != today_str:
            self.state.daily_start_equity = equity
            self.state.pnl_today_pct = 0.0
            self.state.trades_today = 0
            self.state.daily_date = today_str
            logger.info("Daily counters reset for %s (equity=%.2f)", today_str, equity)

    def _check_circuit_breakers(self) -> Optional[str]:
        """Return halt reason string, or None if clear."""
        equity = self.state.equity
        daily_dd = (self.state.daily_start_equity - equity) / max(
            self.state.daily_start_equity, 1e-9
        )
        if daily_dd >= self.daily_loss_limit_pct:
            return f"Daily loss limit reached: {daily_dd*100:.2f}% >= {self.daily_loss_limit_pct*100:.1f}%"

        weekly_dd = (self.state.week_start_equity - equity) / max(
            self.state.week_start_equity, 1e-9
        )
        if weekly_dd >= self.weekly_loss_limit_pct:
            return f"Weekly drawdown limit: {weekly_dd*100:.2f}%"

        monthly_dd = (self.state.month_start_equity - equity) / max(
            self.state.month_start_equity, 1e-9
        )
        if monthly_dd >= self.monthly_dd_limit_pct:
            return f"Monthly drawdown limit: {monthly_dd*100:.2f}%"

        return None

    def _goal_progress(self) -> float:
        """Progress toward monthly goal (0.0 → 1.0+)."""
        start = self.state.month_start_equity
        target = start * (1.0 + self.goal_target_pct)
        if target <= start:
            return 0.0
        progress = (self.state.equity - start) / (target - start)
        return max(0.0, progress)

    def _persist_state(self) -> None:
        """Write state to disk so restarts recover correctly."""
        try:
            self._STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = {
                    "equity": self.state.equity,
                    "peak_equity": self.state.peak_equity,
                    "daily_start_equity": self.state.daily_start_equity,
                    "week_start_equity": self.state.week_start_equity,
                    "month_start_equity": self.state.month_start_equity,
                    "daily_date": self.state.daily_date,
                    "loss_streak": self.state.loss_streak,
                    "win_streak": self.state.win_streak,
                    "trades_today": self.state.trades_today,
                    "pnl_today_pct": self.state.pnl_today_pct,
                }
            with open(self._STATE_PATH, "w") as fh:
                json.dump(data, fh, indent=2)
        except Exception as exc:
            logger.warning("RiskManager: failed to persist state: %s", exc)

    def _load_persisted_state(self, initial_equity: float) -> None:
        """Restore state from disk if available and date matches."""
        try:
            if not self._STATE_PATH.exists():
                return
            with open(self._STATE_PATH) as fh:
                data = json.load(fh)
            today_str = date.today().isoformat()
            if data.get("daily_date") != today_str:
                logger.info("RiskManager: stale persisted state (different day), starting fresh.")
                return
            self.state.equity = float(data.get("equity", initial_equity))
            self.state.peak_equity = float(data.get("peak_equity", initial_equity))
            self.state.daily_start_equity = float(data.get("daily_start_equity", initial_equity))
            self.state.week_start_equity = float(data.get("week_start_equity", initial_equity))
            self.state.month_start_equity = float(data.get("month_start_equity", initial_equity))
            self.state.loss_streak = int(data.get("loss_streak", 0))
            self.state.win_streak = int(data.get("win_streak", 0))
            self.state.trades_today = int(data.get("trades_today", 0))
            self.state.pnl_today_pct = float(data.get("pnl_today_pct", 0.0))
            logger.info(
                "RiskManager: restored state | equity=%.2f streak=%d trades_today=%d",
                self.state.equity, self.state.loss_streak, self.state.trades_today,
            )
        except Exception as exc:
            logger.warning("RiskManager: could not load persisted state: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton — the ONE instance the whole app shares
# ---------------------------------------------------------------------------

_RISK_MANAGER: Optional[RiskManager] = None
_RM_LOCK = threading.Lock()


def get_risk_manager(initial_equity: float = 1000.0) -> RiskManager:
    """
    Return the global RiskManager singleton (lazy-init on first call).

    The initial_equity is only used on the very first call.  Subsequent calls
    return the same instance regardless of the argument.

    In main.py / market-data handler, call:
        rm = get_risk_manager(account_equity_from_ea)
        rm.update_equity(account_equity_from_ea)
    """
    global _RISK_MANAGER
    if _RISK_MANAGER is None:
        with _RM_LOCK:
            if _RISK_MANAGER is None:
                _RISK_MANAGER = RiskManager(initial_equity=initial_equity)
    return _RISK_MANAGER