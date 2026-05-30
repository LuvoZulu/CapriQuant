"""
Dynamic Risk Management for CapriQuant

Designed for aggressive growth targets (e.g. R200 → R17k every 3 weeks)
while preventing instant ruin.

Core philosophy:
- Risk is dynamic based on current equity vs goal trajectory
- Hard circuit breakers always override ambition
- Every trade must have a defined invalidation (structure-based stop)
- Position size is a function of stop distance (ATR or swing)

Never risk what you cannot afford to lose. This module will try to save you from yourself.
"""

from dataclasses import dataclass
from typing import Literal, Optional
import math


@dataclass
class RiskParams:
    account_equity: float
    account_currency: str = "ZAR"

    # Goal context (user wants very fast growth)
    starting_equity: float = 200.0
    target_equity: float = 17000.0
    weeks_target: int = 3

    # Hard risk caps (NEVER exceed these)
    max_risk_per_trade_pct: float = 2.5      # of current equity
    max_daily_loss_pct: float = 6.0          # of starting equity that day
    max_weekly_drawdown_pct: float = 15.0

    # Aggression scaling
    base_risk_pct: float = 1.2               # normal day
    aggressive_risk_pct: float = 2.0         # when behind goal
    conservative_risk_pct: float = 0.7       # when ahead or after loss streak


@dataclass
class TradeRisk:
    symbol: str
    direction: Literal["BUY", "SELL"]
    entry_price: float
    stop_loss: float
    take_profit: Optional[float] = None

    risk_amount: float = 0.0          # in account currency
    risk_pct: float = 0.0             # of equity
    position_size: float = 0.0        # lots or units (depends on instrument)
    rr_ratio: Optional[float] = None


class RiskManager:
    def __init__(self, params: RiskParams):
        self.p = params
        self._daily_loss = 0.0
        self._week_start_equity = params.account_equity
        self._loss_streak = 0

    def current_risk_pct(self, recent_losses: int = 0) -> float:
        """
        Aggressive goal-aware risk sizing (R200 → R17k every 3 weeks target).
        Hard caps are sacred.
        """
        progress = (self.p.account_equity - self.p.starting_equity) / (self.p.target_equity - self.p.starting_equity)
        progress = max(0.0, min(1.0, progress))

        # Extremely aggressive early phase to hit the moonshot goal
        if progress < 0.18:
            base = min(self.p.aggressive_risk_pct, 2.4)
        elif progress < 0.40:
            base = self.p.aggressive_risk_pct
        elif progress > 0.75:
            base = self.p.conservative_risk_pct
        else:
            base = self.p.base_risk_pct

        # Loss streak protection (very important at high aggression)
        penalty = min(0.65, recent_losses * 0.22)
        final = base * (1.0 - penalty)

        return max(0.35, min(final, self.p.max_risk_per_trade_pct))

    def calculate_trade_risk(
        self,
        symbol: str,
        direction: Literal["BUY", "SELL"],
        entry: float,
        stop: float,
        atr: float,
        recent_loss_streak: int = 0,
        account_equity_override: Optional[float] = None,
    ) -> TradeRisk:
        """
        Given a structure-based invalidation (stop), compute safe size.
        Stop should come from market structure (below order block, swing low, etc).
        """
        equity = account_equity_override or self.p.account_equity
        risk_pct = self.current_risk_pct(recent_loss_streak)

        risk_amount = equity * (risk_pct / 100.0)

        # Distance to stop in price
        stop_distance = abs(entry - stop)
        if stop_distance < 0.00001:
            stop_distance = atr * 0.8  # fallback

        # Very rough position sizing (user will map to contract size / lot size in EA)
        # For XAUUSD 1 lot ~ 100 oz, risk per point etc. — EA must translate.
        # We return theoretical units here.
        if stop_distance > 0:
            position_units = risk_amount / stop_distance
        else:
            position_units = 0.0

        rr = None
        if stop != entry:
            # If caller passes TP we could compute real R:R later
            pass

        return TradeRisk(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            stop_loss=stop,
            risk_amount=round(risk_amount, 2),
            risk_pct=round(risk_pct, 2),
            position_size=round(position_units, 4),
            rr_ratio=rr,
        )

    def can_trade_today(self, today_pnl: float, starting_equity_today: float) -> bool:
        """Circuit breaker."""
        daily_loss_pct = (today_pnl / starting_equity_today) * 100.0
        return daily_loss_pct > -self.p.max_daily_loss_pct

    def validate_structure_stop(
        self,
        proposed_stop: float,
        current_price: float,
        nearest_ob_low: Optional[float],
        nearest_ob_high: Optional[float],
        atr: float,
    ) -> float:
        """
        If the proposed stop is too tight (inside an order block or < 0.6 ATR),
        we move it to a more structural level. This is how you survive.
        """
        min_distance = atr * 0.65

        if abs(current_price - proposed_stop) < min_distance:
            if current_price > proposed_stop:  # long
                return current_price - min_distance
            else:
                return current_price + min_distance

        # Prefer stops beyond order blocks when available
        if nearest_ob_low and nearest_ob_high:
            if current_price > nearest_ob_high:  # long above bullish OB
                return min(proposed_stop, nearest_ob_low - (atr * 0.1))
            if current_price < nearest_ob_low:   # short below bearish OB
                return max(proposed_stop, nearest_ob_high + (atr * 0.1))

        return proposed_stop


def suggested_levels_from_structure(
    direction: Literal["BUY", "SELL"],
    entry: float,
    atr: float,
    nearest_ob: Optional[float] = None,
    nearest_liquidity: Optional[float] = None,
    nearest_fvg: Optional[float] = None,
    fib_level: Optional[float] = None,
) -> Dict[str, float]:
    """
    Generate intelligent stop and target suggestions based on structure.
    This is what the EA should use instead of arbitrary pips.
    """
    stop_mult = 1.0
    tp1_mult = 1.8
    tp2_mult = 3.2

    if direction == "BUY":
        stop = entry - (atr * stop_mult)
        tp1 = entry + (atr * tp1_mult)
        tp2 = entry + (atr * tp2_mult)

        # If we have a bullish OB below, put stop just under it
        if nearest_ob and nearest_ob < entry:
            stop = nearest_ob - (atr * 0.15)

        # Target liquidity or fib extension above
        if nearest_liquidity and nearest_liquidity > entry:
            tp2 = nearest_liquidity

    else:  # SELL
        stop = entry + (atr * stop_mult)
        tp1 = entry - (atr * tp1_mult)
        tp2 = entry - (atr * tp2_mult)

        if nearest_ob and nearest_ob > entry:
            stop = nearest_ob + (atr * 0.15)

        if nearest_liquidity and nearest_liquidity < entry:
            tp2 = nearest_liquidity

    return {
        "stop_loss": round(stop, 5),
        "tp1": round(tp1, 5),
        "tp2": round(tp2, 5),
        "rr1": round(abs(tp1 - entry) / abs(stop - entry), 2) if stop != entry else 0,
        "rr2": round(abs(tp2 - entry) / abs(stop - entry), 2) if stop != entry else 0,
    }
