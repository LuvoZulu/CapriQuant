"""
CapriQuant TradeLifecycleManager — Post-Entry Position Management
=================================================================
Fixes:
  - System was entry-only — no management after entry
  - No trailing stop on displacement
  - No break-even trigger on FVG fill / 1R
  - No scale-out on additional confluence
  - No early exit on opposing CHoCH

Usage:
    lifecycle = TradeLifecycleManager()
    lifecycle.register_trade(trade)          # on entry confirmation
    # On each closed bar in your live loop:
    actions = lifecycle.on_bar(bar, market_structure_snapshot)
    for action in actions:
        ea_connector.execute(action)         # send to MT5/EA
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enumerations & action types
# ---------------------------------------------------------------------------

class LifecycleEvent(str, Enum):
    NONE         = "none"
    MOVE_TO_BE   = "move_to_be"
    TRAIL_STOP   = "trail_stop"
    SCALE_OUT    = "scale_out"
    EARLY_EXIT   = "early_exit"
    CLOSE_TP     = "close_tp"
    CLOSE_SL     = "close_sl"


@dataclass
class LifecycleAction:
    """Returned to the caller to execute on the broker/EA side."""
    event: LifecycleEvent
    trade_id: str
    new_stop: Optional[float] = None
    new_tp: Optional[float] = None
    close_lots: Optional[float] = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "event": self.event.value,
            "trade_id": self.trade_id,
            "new_stop": self.new_stop,
            "new_tp": self.new_tp,
            "close_lots": self.close_lots,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Trade state
# ---------------------------------------------------------------------------

@dataclass
class ActiveTrade:
    """
    Represents an open position managed by TradeLifecycleManager.
    Create from your entry signal dict and pass to register_trade().
    """
    trade_id: str
    symbol: str
    direction: str           # 'long' | 'short'
    entry_price: float
    initial_stop: float
    initial_tp: float
    entry_time: datetime
    lot_size: float
    risk_pct: float

    # Mutable state — updated each bar
    current_stop: float = field(init=False)
    current_tp: float = field(init=False)
    remaining_lots: float = field(init=False)
    is_be: bool = False
    scaled_out: bool = False
    partial_exits: List[dict] = field(default_factory=list)
    lifecycle_log: List[str] = field(default_factory=list)
    extreme_price: float = field(init=False)   # highest for longs, lowest for shorts

    def __post_init__(self) -> None:
        self.current_stop = self.initial_stop
        self.current_tp = self.initial_tp
        self.remaining_lots = self.lot_size
        self.extreme_price = self.entry_price

    @property
    def sl_pts(self) -> float:
        return abs(self.entry_price - self.initial_stop)

    @property
    def current_rr(self) -> float:
        if self.sl_pts < 1e-9:
            return 0.0
        if self.direction == "long":
            return max(self.extreme_price - self.entry_price, 0.0) / self.sl_pts
        return max(self.entry_price - self.extreme_price, 0.0) / self.sl_pts

    def log_event(self, msg: str) -> None:
        stamp = datetime.utcnow().strftime("%H:%M:%S")
        entry = f"[{stamp}] {msg}"
        self.lifecycle_log.append(entry)
        logger.info("[%s] %s", self.trade_id, msg)


# ---------------------------------------------------------------------------
# Market structure snapshot (minimal interface matching your existing MS)
# ---------------------------------------------------------------------------

@dataclass
class MarketStructureSnapshot:
    """
    Fill this from your existing MarketStructure object on each bar.
    Your confluence.py / live loop should build one of these per bar.
    """
    last_choch_direction: Optional[str] = None   # 'bullish' | 'bearish'
    last_choch_price: Optional[float] = None
    last_choch_bar: int = 0                       # bar index since CHoCH — ignore stale ones
    displacement_occurred: bool = False
    displacement_direction: Optional[str] = None  # 'bullish' | 'bearish'
    fvg_zones: List[dict] = field(default_factory=list)   # [{high, low, direction, filled}]
    ob_zones: List[dict] = field(default_factory=list)    # [{high, low, direction, mitigated}]
    current_bar_index: int = 0


def snapshot_from_market_structure(ms: Any) -> "MarketStructureSnapshot":
    """
    Adapter: convert real app.features.structure.MarketStructure (or None) into the
    minimal dict-like snapshot that TradeLifecycleManager._check_* methods expect.
    This fixes the broken on_bar calls (real MS has .breaks/.fvgs/.order_blocks not last_choch_*/fvg_zones).
    Safe to call with either shape; returns a usable stub on failure.
    """
    if ms is None:
        return MarketStructureSnapshot()
    try:
        from app.features.structure import MarketStructure as _RealMS
    except Exception:
        _RealMS = None

    # Defaults
    last_choch_direction = None
    last_choch_price = None
    last_choch_bar = 0
    displacement_occurred = False
    displacement_direction = None
    fvg_zones: List[dict] = []
    ob_zones: List[dict] = []
    current_bar_index = getattr(ms, "last_bar_idx", 0) or 0

    # CHOCH from real .breaks (most recent first)
    breaks = getattr(ms, "breaks", None) or []
    for b in reversed(list(breaks)):
        if getattr(b, "break_type", "") == "CHOCH":
            d = getattr(b, "direction", "") or ""
            last_choch_direction = "bearish" if str(d).upper() in ("BEAR", "BEARISH") else ("bullish" if str(d).upper() in ("BULL", "BULLISH") else None)
            last_choch_price = getattr(b, "broken_price", None)
            last_choch_bar = getattr(b, "idx", 0) or 0
            break

    # Displacement (real stores in recent_displacement or infer from bias/breaks)
    rd = getattr(ms, "recent_displacement", None)
    if isinstance(rd, dict):
        displacement_occurred = bool(rd.get("occurred", False))
        dd = str(rd.get("direction", "")).lower()
        displacement_direction = "bullish" if "bull" in dd else ("bearish" if "bear" in dd else None)
    else:
        # Fallback: last break if BOS/CHOCH recent
        if breaks:
            last_b = breaks[-1]
            if getattr(last_b, "break_type", "") in ("BOS", "CHOCH"):
                dd = str(getattr(last_b, "direction", "")).lower()
                displacement_direction = "bullish" if "bull" in dd else ("bearish" if "bear" in dd else None)
                displacement_occurred = True

    # FVG zones (list[dict] shape the checks expect)
    for f in (getattr(ms, "fvgs", None) or []):
        fvg_zones.append({
            "high": getattr(f, "upper", getattr(f, "high", getattr(f, "fvg_high", 0.0))),
            "low": getattr(f, "lower", getattr(f, "low", getattr(f, "fvg_low", 0.0))),
            "direction": "bullish" if str(getattr(f, "fvg_type", "")).upper() == "BULLISH" else "bearish",
            "filled": bool(getattr(f, "is_filled", getattr(f, "filled", False))),
        })

    # OB zones
    for o in (getattr(ms, "order_blocks", None) or []):
        ob_zones.append({
            "high": getattr(o, "high", 0.0),
            "low": getattr(o, "low", 0.0),
            "direction": "bullish" if str(getattr(o, "ob_type", "")).upper() == "BULLISH" else "bearish",
            "mitigated": bool(getattr(o, "is_mitigated", False)),
        })

    return MarketStructureSnapshot(
        last_choch_direction=last_choch_direction,
        last_choch_price=last_choch_price,
        last_choch_bar=last_choch_bar,
        displacement_occurred=displacement_occurred,
        displacement_direction=displacement_direction,
        fvg_zones=fvg_zones,
        ob_zones=ob_zones,
        current_bar_index=current_bar_index,
    )


# ---------------------------------------------------------------------------
# TradeLifecycleManager
# ---------------------------------------------------------------------------

class TradeLifecycleManager:
    """
    Manages open positions bar-by-bar after entry.

    Logic priority per bar (in order):
      1. Check SL/TP hit → close
      2. Check opposing CHoCH → early exit
      3. Check scale-out at N×R → partial close
      4. Check break-even trigger → move stop
      5. Check trailing stop (only after BE is set) → move stop
    """

    def __init__(
        self,
        # Break-even
        be_trigger_r: float = 1.0,
        be_buffer_factor: float = 0.05,       # 5% of SL as buffer above entry
        fvg_be_trigger: bool = True,

        # Trailing
        trail_trigger_r: float = 1.5,
        trail_buffer_pts: float = 3.0,        # Points behind swing / OB
        ob_trail_enabled: bool = True,

        # Scale-out
        scale_out_r: float = 1.0,
        scale_out_pct: float = 0.50,          # Close 50% of position at 1R

        # Early exit
        early_exit_on_choch: bool = True,
        choch_stale_bars: int = 3,            # Ignore CHoCH older than N bars
    ):
        self.be_trigger_r = be_trigger_r
        self.be_buffer_factor = be_buffer_factor
        self.fvg_be_trigger = fvg_be_trigger
        self.trail_trigger_r = trail_trigger_r
        self.trail_buffer_pts = trail_buffer_pts
        self.ob_trail_enabled = ob_trail_enabled
        self.scale_out_r = scale_out_r
        self.scale_out_pct = scale_out_pct
        self.early_exit_on_choch = early_exit_on_choch
        self.choch_stale_bars = choch_stale_bars

        self._trades: Dict[str, ActiveTrade] = {}

    # ------------------------------------------------------------------
    # Trade registration
    # ------------------------------------------------------------------

    def register_trade(self, trade: ActiveTrade) -> None:
        """Call immediately after entry is confirmed by the broker."""
        self._trades[trade.trade_id] = trade
        logger.info(
            "Trade registered | id=%s dir=%s entry=%.5f sl=%.5f tp=%.5f lots=%.2f",
            trade.trade_id, trade.direction, trade.entry_price,
            trade.initial_stop, trade.initial_tp, trade.lot_size,
        )

    def remove_trade(self, trade_id: str) -> None:
        """Call after close confirmation. Automatically called on SL/TP/exit actions."""
        self._trades.pop(trade_id, None)

    @property
    def open_trade_ids(self) -> List[str]:
        return list(self._trades.keys())

    def get_trade(self, trade_id: str) -> Optional[ActiveTrade]:
        return self._trades.get(trade_id)

    # ------------------------------------------------------------------
    # Per-bar update — main entry point from your live loop
    # ------------------------------------------------------------------

    def on_bar(
        self,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_time: datetime,
        ms: MarketStructureSnapshot,
    ) -> List[LifecycleAction]:
        """
        Call on each CLOSED bar for all active trades.
        Returns actions the EA layer must execute (stop moves, partial closes, full closes).

        Wire into your live loop:
            bar = latest_closed_bar()
            ms  = build_ms_snapshot(market_structure)
            actions = lifecycle.on_bar(bar.open, bar.high, bar.low, bar.close, bar.time, ms)
            for action in actions:
                ea.execute_action(action)
        """
        actions: List[LifecycleAction] = []

        # Auto-adapt real MarketStructure objects (from compute_structure) into the stub the checks expect.
        # This is the key fix for previously-silent on_bar failures (no more broad except swallowing everything).
        if not isinstance(ms, MarketStructureSnapshot):
            try:
                ms = snapshot_from_market_structure(ms)
            except Exception as _conv_exc:
                logger.debug("[Lifecycle] snapshot conversion failed, using empty stub: %s", _conv_exc)
                ms = MarketStructureSnapshot()

        for trade_id, trade in list(self._trades.items()):
            # Update price extreme
            if trade.direction == "long":
                trade.extreme_price = max(trade.extreme_price, bar_high)
            else:
                trade.extreme_price = min(trade.extreme_price, bar_low)

            # 1. SL / TP hit
            closed = self._check_closure(trade, bar_high, bar_low)
            if closed:
                actions.append(closed)
                self.remove_trade(trade_id)
                continue

            # 2. Early exit on opposing CHoCH
            if self.early_exit_on_choch:
                exit_action = self._check_choch_exit(trade, ms)
                if exit_action:
                    actions.append(exit_action)
                    self.remove_trade(trade_id)
                    continue

            # 3. Scale-out (once per trade)
            if not trade.scaled_out:
                scale = self._check_scale_out(trade, bar_high, bar_low)
                if scale:
                    actions.append(scale)

            # 4. Break-even (once per trade)
            if not trade.is_be:
                be = self._check_be(trade, bar_high, bar_low, ms)
                if be:
                    actions.append(be)

            # 5. Trailing stop (only after BE)
            if trade.is_be:
                trail = self._check_trail(trade, bar_high, bar_low, ms)
                if trail:
                    actions.append(trail)

        return actions

    # ------------------------------------------------------------------
    # Private check methods
    # ------------------------------------------------------------------

    def _check_closure(
        self, trade: ActiveTrade, bar_high: float, bar_low: float
    ) -> Optional[LifecycleAction]:
        if trade.direction == "long":
            if bar_low <= trade.current_stop:
                trade.log_event(f"SL hit | bar_low={bar_low:.5f} stop={trade.current_stop:.5f}")
                return LifecycleAction(
                    LifecycleEvent.CLOSE_SL, trade.trade_id,
                    close_lots=trade.remaining_lots, reason="sl"
                )
            if bar_high >= trade.current_tp:
                trade.log_event(f"TP hit | bar_high={bar_high:.5f} tp={trade.current_tp:.5f}")
                return LifecycleAction(
                    LifecycleEvent.CLOSE_TP, trade.trade_id,
                    close_lots=trade.remaining_lots, reason="tp"
                )
        else:  # short
            if bar_high >= trade.current_stop:
                trade.log_event(f"SL hit | bar_high={bar_high:.5f} stop={trade.current_stop:.5f}")
                return LifecycleAction(
                    LifecycleEvent.CLOSE_SL, trade.trade_id,
                    close_lots=trade.remaining_lots, reason="sl"
                )
            if bar_low <= trade.current_tp:
                trade.log_event(f"TP hit | bar_low={bar_low:.5f} tp={trade.current_tp:.5f}")
                return LifecycleAction(
                    LifecycleEvent.CLOSE_TP, trade.trade_id,
                    close_lots=trade.remaining_lots, reason="tp"
                )
        return None

    def _check_be(
        self,
        trade: ActiveTrade,
        bar_high: float,
        bar_low: float,
        ms: MarketStructureSnapshot,
    ) -> Optional[LifecycleAction]:
        triggered = False
        reason = ""

        # R-based trigger
        if trade.current_rr >= self.be_trigger_r:
            triggered = True
            reason = f"BE at {trade.current_rr:.2f}R"

        # FVG mitigation trigger (entry FVG starts to fill against us)
        if self.fvg_be_trigger and not triggered:
            for fvg in ms.fvg_zones:
                fvg_dir = fvg.get("direction", "")
                # If there's a bearish FVG above entry on a long → BE when it fills
                if trade.direction == "long" and fvg_dir == "bearish":
                    if bar_high >= fvg.get("low", float("inf")):
                        triggered = True
                        reason = "BE: bearish FVG above being filled"
                        break
                elif trade.direction == "short" and fvg_dir == "bullish":
                    if bar_low <= fvg.get("high", 0.0):
                        triggered = True
                        reason = "BE: bullish FVG below being filled"
                        break

        if not triggered:
            return None

        # Set stop just beyond entry with small noise buffer
        buffer = trade.sl_pts * self.be_buffer_factor
        if trade.direction == "long":
            new_stop = round(trade.entry_price + buffer, 5)
        else:
            new_stop = round(trade.entry_price - buffer, 5)

        # Only move if it's actually better than current stop
        if trade.direction == "long" and new_stop <= trade.current_stop:
            return None
        if trade.direction == "short" and new_stop >= trade.current_stop:
            return None

        trade.current_stop = new_stop
        trade.is_be = True
        trade.log_event(f"BE moved | stop→{new_stop:.5f} | {reason}")

        return LifecycleAction(
            LifecycleEvent.MOVE_TO_BE, trade.trade_id,
            new_stop=new_stop, reason=reason
        )

    def _check_trail(
        self,
        trade: ActiveTrade,
        bar_high: float,
        bar_low: float,
        ms: MarketStructureSnapshot,
    ) -> Optional[LifecycleAction]:
        if trade.current_rr < self.trail_trigger_r:
            return None

        new_stop = trade.current_stop

        # --- Displacement-based trail ---
        if ms.displacement_occurred and ms.displacement_direction == (
            "bullish" if trade.direction == "long" else "bearish"
        ):
            if trade.direction == "long":
                candidate = round(bar_low - self.trail_buffer_pts, 5)
                if candidate > new_stop:
                    new_stop = candidate
            else:
                candidate = round(bar_high + self.trail_buffer_pts, 5)
                if candidate < new_stop:
                    new_stop = candidate

        # --- OB-based trail ---
        if self.ob_trail_enabled:
            for ob in ms.ob_zones:
                if ob.get("mitigated"):
                    continue
                ob_dir = ob.get("direction", "")
                if trade.direction == "long" and ob_dir == "bullish":
                    candidate = round(ob.get("low", 0.0) - self.trail_buffer_pts, 5)
                    if candidate > new_stop:
                        new_stop = candidate
                elif trade.direction == "short" and ob_dir == "bearish":
                    candidate = round(ob.get("high", float("inf")) + self.trail_buffer_pts, 5)
                    if candidate < new_stop:
                        new_stop = candidate

        if new_stop == trade.current_stop:
            return None

        trade.current_stop = new_stop
        trade.log_event(f"Trail | stop→{new_stop:.5f} | rr={trade.current_rr:.2f}R")

        return LifecycleAction(
            LifecycleEvent.TRAIL_STOP, trade.trade_id,
            new_stop=new_stop, reason=f"structure_trail rr={trade.current_rr:.2f}R"
        )

    def _check_scale_out(
        self, trade: ActiveTrade, bar_high: float, bar_low: float
    ) -> Optional[LifecycleAction]:
        if trade.current_rr < self.scale_out_r:
            return None

        lots_to_close = round(trade.remaining_lots * self.scale_out_pct, 2)
        if lots_to_close <= 0:
            return None

        trade.remaining_lots = round(trade.remaining_lots - lots_to_close, 2)
        trade.scaled_out = True
        trade.partial_exits.append({
            "reason": "scale_out",
            "lots": lots_to_close,
            "rr_at_exit": round(trade.current_rr, 2),
        })
        trade.log_event(f"Scale-out {lots_to_close} lots @ {trade.current_rr:.2f}R")

        return LifecycleAction(
            LifecycleEvent.SCALE_OUT, trade.trade_id,
            close_lots=lots_to_close,
            reason=f"scale_out_{self.scale_out_r}R"
        )

    def _check_choch_exit(
        self, trade: ActiveTrade, ms: MarketStructureSnapshot
    ) -> Optional[LifecycleAction]:
        if ms.last_choch_direction is None:
            return None

        # Ignore stale CHoCH signals
        bars_since = ms.current_bar_index - ms.last_choch_bar
        if bars_since > self.choch_stale_bars:
            return None

        opposing = "bearish" if trade.direction == "long" else "bullish"
        if ms.last_choch_direction != opposing:
            return None

        # Don't exit if CHoCH is within our SL range (let SL handle it)
        if ms.last_choch_price is not None:
            if trade.direction == "long" and ms.last_choch_price <= trade.current_stop:
                return None
            if trade.direction == "short" and ms.last_choch_price >= trade.current_stop:
                return None

        # Must be in profit or at BE for early exit to make sense
        if trade.current_rr < 0:
            return None

        trade.log_event(
            f"Early exit | opposing CHoCH {ms.last_choch_direction} "
            f"@ {ms.last_choch_price} | rr={trade.current_rr:.2f}R"
        )
        return LifecycleAction(
            LifecycleEvent.EARLY_EXIT, trade.trade_id,
            close_lots=trade.remaining_lots,
            reason=f"choch_{ms.last_choch_direction}"
        )


# ---------------------------------------------------------------------------
# Adapter: allow the *real* MarketStructure (from features.structure) to drive
# lifecycle checks. The Snapshot was a minimal stub; without this the
# fvg_zones / ob_zones / choch / displacement attrs were missing and all
# post-entry actions were silently skipped (caught in caller).
# ---------------------------------------------------------------------------

def build_snapshot_from_ms(ms: object) -> MarketStructureSnapshot:
    """Convert real MarketStructure object into the dict-based Snapshot the
    lifecycle checks expect. This makes BE / trail / scale / choch-exit actually
    execute in the live backend instead of always returning no actions.
    """
    if ms is None:
        return MarketStructureSnapshot()

    breaks = getattr(ms, "breaks", []) or []
    last_choch_direction = None
    last_choch_price = None
    last_choch_bar = getattr(ms, "last_bar_idx", 0)
    for b in reversed(breaks):
        if getattr(b, "break_type", "") == "CHOCH":
            d = getattr(b, "direction", "") or ""
            last_choch_direction = d.lower() if d else None
            last_choch_price = getattr(b, "broken_price", None)
            break

    disp = getattr(ms, "recent_displacement", None) or {}
    displacement_occurred = bool(disp)
    disp_dir = (disp.get("direction") or "") if isinstance(disp, dict) else ""
    displacement_direction = disp_dir.lower() if disp_dir else None

    fvg_zones = []
    for f in getattr(ms, "fvgs", []) or []:
        fvg_zones.append({
            "high": float(getattr(f, "high", getattr(f, "upper", 0) or 0)),
            "low": float(getattr(f, "low", getattr(f, "lower", 0) or 0)),
            "direction": str(getattr(f, "fvg_type", "")).lower(),
            "filled": bool(getattr(f, "is_filled", False)),
        })

    ob_zones = []
    for o in getattr(ms, "order_blocks", []) or []:
        ob_zones.append({
            "high": float(getattr(o, "high", 0) or 0),
            "low": float(getattr(o, "low", 0) or 0),
            "direction": str(getattr(o, "ob_type", "")).lower(),
            "mitigated": bool(getattr(o, "is_mitigated", False)),
        })

    return MarketStructureSnapshot(
        last_choch_direction=last_choch_direction,
        last_choch_price=last_choch_price,
        last_choch_bar=last_choch_bar,
        displacement_occurred=displacement_occurred,
        displacement_direction=displacement_direction,
        fvg_zones=fvg_zones,
        ob_zones=ob_zones,
        current_bar_index=getattr(ms, "last_bar_idx", 0),
    )