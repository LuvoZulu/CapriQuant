"""
CapriQuant RiskManager — Full Production Implementation
=======================================================
Fixes:
  - RiskManager was implemented but NEVER instantiated in live signal path
  - apply_m5_risk_levels was a simplified fallback (now replaced)
  - EA was falling back to hardcoded risk (1.8–2.5%) without any circuits
  - No live equity / streak / daily PnL feeding back into decisions

Usage (in your live signal path / confluence.py):
    rm = RiskManager(initial_equity=10_000)
    risk_pct = rm.get_risk_pct(setup_quality=0.85)
    if risk_pct is None:
        return  # halted — do not trade
    validated = rm.validate_structure_stop(entry, stop, tp, direction)
    if not validated["valid"]:
        return
    lots = rm.compute_position_size(equity, risk_pct, validated["sl_pts"], pip_value)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

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
    daily_date: date
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
    Production risk manager. Instantiate ONCE at bot startup and keep alive.

    Key circuits:
      1. Daily loss circuit breaker (default 3%)
      2. Weekly drawdown guard (default 6%)
      3. Monthly drawdown guard (default 10%)
      4. Daily trade count cap
      5. Loss-streak exponential de-risking
      6. Goal-progress de-risking (avoid giving back a winning month)
      7. Setup quality scaling
      8. Structure stop validation with R:R gating
    """

    def __init__(
        self,
        initial_equity: float,
        base_risk_pct: float = 0.010,         # 1.0% base
        max_risk_pct: float = 0.015,           # 1.5% ceiling — NOT 2.5%
        min_risk_pct: float = 0.003,           # 0.3% floor
        daily_loss_limit_pct: float = 0.030,   # 3%  daily  → halt
        weekly_loss_limit_pct: float = 0.060,  # 6%  weekly → halt
        monthly_dd_limit_pct: float = 0.100,   # 10% monthly→ halt
        max_daily_trades: int = 6,
        min_rr: float = 1.5,
        streak_penalty_factor: float = 0.20,   # 20% reduction per consecutive loss
        max_streak_penalty: float = 0.60,      # cap at 60% total penalty
        goal_target_pct: float = 0.10,         # 10% monthly goal
        goal_derisking_start: float = 0.70,    # start de-risking at 70% of goal
        goal_min_scale: float = 0.40,          # minimum scale when at 100% goal
    ):
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

        today = date.today()
        self.state = RiskState(
            equity=initial_equity,
            peak_equity=initial_equity,
            daily_start_equity=initial_equity,
            daily_date=today,
        )
        self._month_start_equity: float = initial_equity
        self._week_start_equity: float = initial_equity
        self._executed_trades: list[TradeRecord] = []

        logger.info(
            "RiskManager initialised | equity=%.2f base=%.1f%% max=%.1f%% "
            "daily_limit=%.1f%% monthly_dd=%.1f%%",
            initial_equity,
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
        Call after every trade close OR on broker heartbeat.
        This is what connects live equity into every risk decision.
        """
        self._refresh_daily(new_equity)
        self.state.equity = new_equity
        if new_equity > self.state.peak_equity:
            self.state.peak_equity = new_equity

    def record_trade(self, trade: TradeRecord) -> None:
        """
        Register a completed trade. Updates streak + daily PnL.
        Call immediately after close confirmation from broker.
        """
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

    def get_risk_pct(self, setup_quality: float = 1.0) -> Optional[float]:
        """
        Returns the risk % for the next trade, or None if trading must halt.

        setup_quality — float [0.0, 1.0] from your confluence scorer.
                        Pass 1.0 if you don't have a confluence score yet.

        Replace every call to apply_m5_risk_levels() with this.
        """
        self._refresh_daily(self.state.equity)

        # --- Circuit breakers first ---
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

        # --- Loss-streak penalty (exponential feel, linear formula) ---
        streak_penalty = min(
            self.state.loss_streak * self.streak_penalty_factor,
            self.max_streak_penalty,
        )
        risk *= 1.0 - streak_penalty

        # --- Goal-progress de-risking ---
        goal_progress = self._goal_progress()
        if goal_progress >= self.goal_derisking_start:
            # Linear: 100% scale at goal_derisking_start → goal_min_scale at 100% goal
            t = (goal_progress - self.goal_derisking_start) / (1.0 - self.goal_derisking_start)
            scale = 1.0 - t * (1.0 - self.goal_min_scale)
            risk *= max(scale, self.goal_min_scale)

        # --- Setup quality scaling (never penalise below 50%) ---
        quality_clipped = max(setup_quality, 0.50)
        risk *= quality_clipped

        # --- Clamp ---
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
    ) -> dict:
        """
        Validates entry / stop / TP structure.
        Returns dict with keys: valid, rr, sl_pts, tp_pts, reason, adjusted_tp.

        Wire this in BEFORE sending any order to the EA.
        """
        if direction not in ("long", "short"):
            return {"valid": False, "reason": "Invalid direction"}

        sl_pts = abs(entry - stop)
        if sl_pts < 1e-9:
            return {"valid": False, "reason": "Zero-width stop"}

        # Spread-adjusted effective risk and reward
        effective_risk = sl_pts + spread_pts
        tp_pts = abs(tp - entry)
        net_tp = tp_pts - spread_pts

        if net_tp <= 0:
            return {"valid": False, "reason": "TP inside spread", "rr": 0.0}

        rr = net_tp / effective_risk

        if rr < self.min_rr:
            # Suggest widened TP to achieve minimum R:R
            required_net_tp = effective_risk * self.min_rr + spread_pts
            if direction == "long":
                adj_tp = entry + required_net_tp
            else:
                adj_tp = entry - required_net_tp
            return {
                "valid": False,
                "rr": round(rr, 2),
                "reason": f"R:R {rr:.2f} below minimum {self.min_rr}",
                "adjusted_tp": round(adj_tp, 5),
            }

        return {
            "valid": True,
            "rr": round(rr, 2),
            "sl_pts": round(sl_pts, 5),
            "tp_pts": round(net_tp, 5),
            "reason": "OK",
        }

    def compute_position_size(
        self,
        equity: float,
        risk_pct: float,
        sl_pts: float,
        pip_value: float = 1.0,
        min_lots: float = 0.01,
        max_lots: float = 10.0,
    ) -> float:
        """
        Computes lot size.
        pip_value: $ per 1.0 lot per 1 point (e.g. XAUUSD = $1 per lot per $0.01 point).

        For XAUUSD on MT5 where 1 lot = 100 oz:
            pip_value = 1.0  (price in USD, 1 point = $0.01, 1 lot = $1 per point)
        """
        if sl_pts <= 0 or pip_value <= 0 or equity <= 0:
            return min_lots
        risk_amount = equity * risk_pct
        raw_lots = risk_amount / (sl_pts * pip_value)
        lots = round(max(min_lots, min(raw_lots, max_lots)), 2)
        logger.debug(
            "Position size: equity=%.2f risk=%.2f%% sl=%.3f pip_val=%.2f → %.2f lots",
            equity, risk_pct * 100, sl_pts, pip_value, lots,
        )
        return lots

    def get_status(self) -> dict:
        """
        Returns current risk state as a serialisable dict.
        Log this on each signal evaluation for observability.
        """
        return {
            "equity": round(self.state.equity, 2),
            "peak_equity": round(self.state.peak_equity, 2),
            "daily_pnl_pct": round(self.state.pnl_today_pct * 100, 2),
            "monthly_dd_pct": round(self._monthly_dd() * 100, 2),
            "loss_streak": self.state.loss_streak,
            "win_streak": self.state.win_streak,
            "trades_today": self.state.trades_today,
            "is_halted": self.state.is_halted,
            "halt_reason": self.state.halt_reason,
            "goal_progress_pct": round(self._goal_progress() * 100, 1),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_daily(self, equity: float) -> None:
        """Reset daily accumulators on a new calendar day."""
        today = date.today()
        if today != self.state.daily_date:
            logger.info(
                "New trading day. Prior PnL: %.2f%%. Resetting daily state.",
                self.state.pnl_today_pct * 100,
            )
            self.state.daily_start_equity = equity
            self.state.pnl_today_pct = 0.0
            self.state.trades_today = 0
            self.state.daily_date = today

    def _check_circuit_breakers(self) -> Optional[str]:
        """Returns a halt reason string, or None if all clear."""
        eq = self.state.equity

        # Daily loss
        daily_loss = (self.state.daily_start_equity - eq) / self.state.daily_start_equity
        if daily_loss >= self.daily_loss_limit_pct:
            return (
                f"Daily loss {daily_loss*100:.1f}% >= limit "
                f"{self.daily_loss_limit_pct*100:.0f}%"
            )

        # Weekly loss
        weekly_loss = (self._week_start_equity - eq) / self._week_start_equity
        if weekly_loss >= self.weekly_loss_limit_pct:
            return (
                f"Weekly loss {weekly_loss*100:.1f}% >= limit "
                f"{self.weekly_loss_limit_pct*100:.0f}%"
            )

        # Monthly drawdown from peak
        monthly_dd = self._monthly_dd()
        if monthly_dd >= self.monthly_dd_limit_pct:
            return (
                f"Monthly DD {monthly_dd*100:.1f}% >= limit "
                f"{self.monthly_dd_limit_pct*100:.0f}%"
            )

        return None

    def _monthly_dd(self) -> float:
        if self._month_start_equity <= 0:
            return 0.0
        return max(
            0.0,
            (self._month_start_equity - self.state.equity) / self._month_start_equity,
        )

    def _goal_progress(self) -> float:
        """0.0 = no progress toward monthly goal. 1.0 = goal fully achieved."""
        if self.goal_target_pct <= 0 or self._month_start_equity <= 0:
            return 0.0
        gain = (self.state.equity - self._month_start_equity) / self._month_start_equity
        return min(max(gain / self.goal_target_pct, 0.0), 1.0)