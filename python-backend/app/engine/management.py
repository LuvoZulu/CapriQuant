"""
Post-Entry Management Engine for CapriQuant.

Computes structure-based actions for open trades:
- MOVE_BE: move SL to breakeven (or slight profit) when entry structure (OB/FVG) is mitigated in favorable direction.
- TRAIL_SL: move SL to recent swing in direction + pad.
- CLOSE: early exit on opposing CHOCH or strong reversal structure.
- SCALE: suggestion for additional size on new confluence (not auto for safety).

Designed to be called from realtime paths or /api for open trades.
Non-bypassable: respects system mode (no management if paused/flatten).
Integrates with RiskManager indirectly (management can tighten risk).

Keep rules conservative at first.
"""

from typing import Dict, List, Optional, Any
from datetime import datetime

from app.features.structure import MarketStructure
from app.engine.confluence import generate_structure_summary

def _get_entry_context(trade: Dict) -> Dict:
    """Extract or default entry context from stored trade (notes or future column)."""
    notes = trade.get("notes") or ""
    # Simple: if notes has json-like, but for now use entry_price and assume.
    # In future, when opening we can store entry_ob_price etc.
    return {
        "entry_price": trade.get("entry_price"),
        "entry_sl": trade.get("stop_loss"),
        "entry_tp": trade.get("tp1") or trade.get("tp2"),
    }

def _is_ob_mitigated_for_trade(ms: MarketStructure, direction: str, entry_price: float) -> bool:
    """Heuristic: was there a relevant OB at entry that is now mitigated and price has moved favorably?"""
    if not ms.order_blocks:
        return False
    entry_price = float(entry_price or ms.current_price)
    for ob in ms.order_blocks:
        if ob.is_mitigated:
            if direction == "BUY" and ob.ob_type == "BULLISH":
                # For long entered above bullish OB, if OB mitigated (price came back and pushed through) and now price > entry
                if ob.high < entry_price and ms.current_price > entry_price + (ms.atr * 0.1):
                    return True
            if direction == "SELL" and ob.ob_type == "BEARISH":
                if ob.low > entry_price and ms.current_price < entry_price - (ms.atr * 0.1):
                    return True
    return False

def _is_fvg_mitigated_favorably(ms: MarketStructure, direction: str, entry_price: float) -> bool:
    """Check if a FVG related to entry is filled and price has continued."""
    entry_price = float(entry_price or ms.current_price)
    for fvg in getattr(ms, "fvgs", []):
        if getattr(fvg, "is_filled", False):
            if direction == "BUY" and fvg.fvg_type == "BULLISH":
                if fvg.high < entry_price and ms.current_price > entry_price:
                    return True
            if direction == "SELL" and fvg.fvg_type == "BEARISH":
                if fvg.low > entry_price and ms.current_price < entry_price:
                    return True
    return False

def _has_opposing_choch(ms: MarketStructure, direction: str) -> bool:
    """Strong opposing structure break."""
    recent_breaks = getattr(ms, "breaks", [])[-3:] if getattr(ms, "breaks", None) else []
    for b in recent_breaks:
        if direction == "BUY" and getattr(b, "break_type", "") == "CHOCH" and getattr(b, "direction", "") == "BEAR":
            return True
        if direction == "SELL" and getattr(b, "break_type", "") == "CHOCH" and getattr(b, "direction", "") == "BULL":
            return True
    # Also if current bias strongly against
    bias = getattr(ms, "bias", "NEUTRAL")
    if direction == "BUY" and bias == "BEARISH":
        return True
    if direction == "SELL" and bias == "BULLISH":
        return True
    return False

def _recent_swing_for_trail(ms: MarketStructure, direction: str) -> Optional[float]:
    """Find recent swing to trail to."""
    swings = getattr(ms, "swings", [])
    if not swings:
        return None
    atr = getattr(ms, "atr", 0) or (ms.current_price * 0.001)
    pad = 0.3 * atr
    if direction == "BUY":
        recent_lows = [s.price for s in swings if s.swing_type == "LOW"][-2:]
        if recent_lows:
            return min(recent_lows) - pad
    else:
        recent_highs = [s.price for s in swings if s.swing_type == "HIGH"][-2:]
        if recent_highs:
            return max(recent_highs) + pad
    return None

def compute_management_for_open(trade: Dict, ms: MarketStructure, system_mode: str = "trading") -> Optional[Dict]:
    """
    Given an open trade dict (from /api/open-trades) and current MarketStructure for the symbol,
    return a suggested management action or None.
    """
    if system_mode != "trading":
        return None  # no management actions while paused or flattening

    if not trade or not ms:
        return None

    ticket = trade.get("ticket")
    direction = str(trade.get("direction", "")).upper()
    if direction not in ("BUY", "SELL") or not ticket:
        return None

    entry_ctx = _get_entry_context(trade)
    entry_price = entry_ctx.get("entry_price") or trade.get("entry_price")
    current_price = getattr(ms, "current_price", 0)

    if not entry_price or current_price == 0:
        return None

    atr = getattr(ms, "atr", 0) or abs(current_price - (trade.get("stop_loss") or current_price)) * 0.8 or (current_price * 0.001)

    actions = []

    # 1. Breakeven on mitigation (very high priority for risk reduction)
    if _is_ob_mitigated_for_trade(ms, direction, entry_price) or _is_fvg_mitigated_favorably(ms, direction, entry_price):
        pad = 0.15 * atr
        new_sl = entry_price + pad if direction == "BUY" else entry_price - pad
        # Only if better than current SL
        current_sl = trade.get("stop_loss") or 0
        if (direction == "BUY" and new_sl > current_sl) or (direction == "SELL" and new_sl < current_sl):
            actions.append({
                "action": "MOVE_BE",
                "new_sl": round(new_sl, 5),
                "reason": "Entry structure (OB/FVG) mitigated + favorable move",
                "confidence": 0.75,
            })

    # 2. Opposing structure -> consider early close
    if _has_opposing_choch(ms, direction):
        actions.append({
            "action": "CLOSE",
            "reason": "Opposing CHOCH or strong reversal structure",
            "confidence": 0.8,
        })

    # 3. Trail to recent swing (if no stronger action)
    if not actions or actions[0]["action"] == "MOVE_BE":
        trail = _recent_swing_for_trail(ms, direction)
        current_sl = trade.get("stop_loss") or 0
        if trail:
            if (direction == "BUY" and trail > current_sl + 0.1*atr) or (direction == "SELL" and trail < current_sl - 0.1*atr):
                actions.append({
                    "action": "TRAIL_SL",
                    "new_sl": round(trail, 5),
                    "reason": "Trail to recent swing + pad",
                    "confidence": 0.65,
                })

    if not actions:
        return None

    # Prefer the highest confidence / most protective first
    best = sorted(actions, key=lambda a: -a.get("confidence", 0))[0]
    best.update({
        "ticket": ticket,
        "symbol": trade.get("symbol"),
        "direction": direction,
        "current_price": round(current_price, 5),
        "current_sl": trade.get("stop_loss"),
        "ts": datetime.utcnow().isoformat(),
        "structure_summary": generate_structure_summary(ms),
    })
    return best

def compute_managements_for_all_opens(open_trades: List[Dict], live_structures: Dict[str, MarketStructure], system_mode: str = "trading") -> List[Dict]:
    """Batch compute for all opens."""
    results = []
    for trade in open_trades or []:
        sym = trade.get("symbol")
        ms = live_structures.get(sym)
        if ms:
            mgmt = compute_management_for_open(trade, ms, system_mode)
            if mgmt:
                results.append(mgmt)
    return results
