"""
app/risk/__init__.py
====================
Integration fix: export the NEW production RiskManager (risk_manager.py)
as the default, while keeping the old RiskParams/TradeRisk shim available
for the few callers (multi_timeframe.py, main.py) that still use RiskParams.

WHY THIS FILE WAS CHANGED
--------------------------
The old __init__.py exported only manager.RiskManager (which had a can_take_trade
API) but the new production class lives in risk_manager.py and has the full
circuit-breaker suite.  This file re-exports everything so all import sites
keep working without any changes to their own import lines.
"""

# ── New production RiskManager (full circuit-breakers, singleton-safe) ───────
from .risk_manager import RiskManager, RiskState, TradeRecord

# ── Legacy shim kept for callers that pass RiskParams to old can_take_trade ──
# main.py and multi_timeframe.py instantiate RiskManager(params) with the old
# .can_take_trade() API.  We patch that in here so those callers still work
# without touching their own code.
from .manager import RiskParams, TradeRisk, suggested_levels_from_structure

# Monkey-patch can_take_trade onto the new RiskManager so legacy call-sites
# (main.py, multi_timeframe.py) continue to work unchanged.
# Signature: can_take_trade(recent_loss_streak, today_pnl, starting_equity_today)
# -> (allowed: bool, reason: str, risk_pct: float)
def _can_take_trade(self, recent_loss_streak: int = 0,
                    today_pnl: float = 0.0,
                    starting_equity_today: float = 0.0) -> tuple:
    """
    Legacy compatibility shim.
    Delegates to get_risk_pct() + _check_circuit_breakers().
    Called by main.py and multi_timeframe.py with the old (params) constructor.
    """
    import logging
    _log = logging.getLogger(__name__)

    # Sync streak into state so circuit-breakers see it
    self.state.loss_streak = recent_loss_streak

    # Sync daily PnL so the daily-loss circuit fires correctly
    if starting_equity_today > 0 and today_pnl != 0.0:
        # today_pnl is already in $; convert to fraction
        self.state.daily_start_equity = starting_equity_today
        self.state.pnl_today_pct = today_pnl / starting_equity_today

    halt_reason = self._check_circuit_breakers()
    if halt_reason:
        self.state.is_halted = True
        self.state.halt_reason = halt_reason
        _log.warning("TRADING HALTED (legacy shim): %s", halt_reason)
        return False, halt_reason, 0.0

    risk_pct = self.get_risk_pct(setup_quality=1.0)
    if risk_pct is None:
        return False, "Daily trade cap or halt", 0.0

    # Convert from fraction (0.01) to percentage (1.0) for legacy callers
    return True, "OK", round(risk_pct * 100, 2)


# Only attach if not already present (idempotent)
if not hasattr(RiskManager, "can_take_trade"):
    RiskManager.can_take_trade = _can_take_trade


# ── Constructor compatibility ─────────────────────────────────────────────────
# Legacy callers do: RiskManager(params)  where params is a RiskParams dataclass.
# New constructor is: RiskManager(initial_equity, base_risk_pct, …)
# We wrap __init__ to accept both calling conventions.
_original_init = RiskManager.__init__

def _compat_init(self, params_or_equity=None, *args, **kwargs):
    if isinstance(params_or_equity, RiskParams):
        p = params_or_equity
        _original_init(
            self,
            initial_equity=p.account_equity,
            base_risk_pct=p.base_risk_pct / 100.0,
            max_risk_pct=p.max_risk_per_trade_pct / 100.0,
            daily_loss_limit_pct=p.max_daily_loss_pct / 100.0,
            **{k: v for k, v in kwargs.items()
               if k not in ("initial_equity", "base_risk_pct",
                            "max_risk_pct", "daily_loss_limit_pct")},
        )
    else:
        _original_init(self, params_or_equity, *args, **kwargs)


RiskManager.__init__ = _compat_init

__all__ = [
    "RiskManager",
    "RiskState",
    "TradeRecord",
    "RiskParams",
    "TradeRisk",
    "suggested_levels_from_structure",
]