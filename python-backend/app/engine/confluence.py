"""
Confluence Engine — The new decision system for CapriQuant.

Replaces naive weighted average of 6 weak strategies with explicit,
explainable, high-confluence setup detection based on real market structure.

Philosophy:
- A trade is only valid when 3+ independent, high-quality reasons align.
- Structure (BOS/OB/liquidity) is the foundation.
- Session AMD dynamics provide the timing filter.
- Price action and Fibonacci only matter at structural levels.
- Veto any setup that violates higher-timeframe structure.

This is what allows automatic trading without MACD/RSI/EMA soup.
"""

from typing import Dict, List, Literal, Optional
from dataclasses import dataclass
from app.features.structure import MarketStructure, OrderBlock

# Import the fully rewritten contextual analyzers
from app.strategies import amd, fibonacci, price_action, liquidity
from app.utils.signal_logger import log_signal


@dataclass
class Setup:
    name: str
    direction: Literal["BUY", "SELL"]
    score: float                    # 0.0 - 1.0 conviction
    confluences: List[str]
    entry_zone: tuple[float, float]
    stop_suggestion: float
    tp1: float
    tp2: float
    rationale: str
    veto_reason: Optional[str] = None


def _nearest_active_ob(ms: MarketStructure, direction: str) -> Optional[OrderBlock]:
    active = [ob for ob in ms.order_blocks if not ob.is_mitigated]
    if not active:
        return None
    if direction == "BUY":
        bulls = [ob for ob in active if ob.ob_type == "BULLISH"]
        return min(bulls, key=lambda o: abs(o.high - ms.current_price)) if bulls else None
    else:
        bears = [ob for ob in active if ob.ob_type == "BEARISH"]
        return min(bears, key=lambda o: abs(o.low - ms.current_price)) if bears else None


def evaluate_setups(ms: MarketStructure, spread: float = 0.0) -> List[Setup]:
    """
    Revamped philosophy (Long-term reliability + reasonable frequency)

    Goals:
    - Positive expectancy with controlled drawdowns across regimes
    - Respect higher-timeframe structure and BOS (don't fight the trend)
    - Take more trades than ultra-strict reversal-only mode (target 30-80+/year)
    - Don't miss strong trends (add continuation setups)
    - Still require real structure (not random scalping)

    We lowered some hard gates and penalties while adding better regime awareness
    and trend participation.
    """
    setups: List[Setup] = []
    price = ms.current_price
    atr = ms.atr if ms.atr > 0 else max(0.0001, price * 0.0008)

    # ------------------------------------------------------------------
    # CONTEXTUAL ANALYZERS
    # ------------------------------------------------------------------
    amd_score = amd.analyze_amd_structure(ms)
    fib_score = fibonacci.analyze_fib_confluence(ms)
    pa_score = price_action.analyze_price_action_contextual(ms)
    liq_score = liquidity.analyze_liquidity_sweeps(ms)

    total_confluence = amd_score + fib_score + pa_score + liq_score

    # Global quality gate - lowered for frequency while still requiring decent confluence
    if total_confluence < 0.48:
        return []

    # Simple volatility regime awareness (helps avoid trading in dead/choppy markets)
    recent_atr = atr
    avg_atr = sum([ (ms.swings[i].price - ms.swings[i-1].price if i > 0 else 0) for i in range(1, min(8, len(ms.swings))) ]) / max(1, min(7, len(ms.swings)-1)) or atr
    vol_regime = "high" if recent_atr > avg_atr * 1.15 else "low" if recent_atr < avg_atr * 0.75 else "normal"

    # ------------------------------------------------------------------
    # VETO / PENALTY CONDITIONS (more balanced than previous ultra-strict version)
    # ------------------------------------------------------------------
    def has_veto(direction: str) -> Optional[str]:
        # Strong veto on recent CHOCH against us
        recent_choch = [b for b in ms.breaks[-3:] if b.break_type == "CHOCH"]
        for b in recent_choch:
            if (direction == "BUY" and b.direction == "BEAR") or (direction == "SELL" and b.direction == "BULL"):
                return f"Recent CHOCH against {direction}"

        # BOS awareness (Option A) - now a soft penalty instead of hard veto in most cases
        has_supporting_bos = any(
            b.break_type == "BOS" and b.direction == ("BULL" if direction == "BUY" else "BEAR")
            for b in ms.breaks[-6:]
        )

        # Only hard veto if no BOS + very weak confluence
        if not has_supporting_bos and total_confluence < 0.65:
            return "No recent BOS + weak confluence"

        # Respect HTF bias (Option C) but not as absolute
        if ms.bias == "BEARISH" and direction == "BUY" and total_confluence < 1.1:
            return "Against bearish market structure"
        if ms.bias == "BULLISH" and direction == "SELL" and total_confluence < 1.1:
            return "Against bullish market structure"

        return None

    # ------------------------------------------------------------------
    # SETUP 1: Order Block Retacements (still high quality)
    # ------------------------------------------------------------------
    for ob in [o for o in ms.order_blocks if not o.is_mitigated]:
        if ob.ob_type == "BULLISH":
            near_ob = abs(price - ob.high) < atr * 0.5 or (ob.low <= price <= ob.high)
            decent_session = ms.session.phase in ("LONDON_OPEN", "NY_OPEN", "NY_PM") or vol_regime == "high"
            decent_amd = amd_score > 0.15

            if near_ob and (decent_session or decent_amd):
                veto = has_veto("BUY")
                if not veto:
                    extra = " + manipulation" if ms.session.manipulation_detected else ""
                    score = 0.72 + (0.1 if ms.session.manipulation_detected else 0) + (0.08 if vol_regime == "high" else 0)
                    setups.append(Setup(
                        name="OB_RETRACEMENT",
                        direction="BUY",
                        score=min(0.92, score),
                        confluences=["BULLISH_OB", ms.session.phase, "STRUCTURE"],
                        entry_zone=(ob.low, ob.high),
                        stop_suggestion=ob.low - (atr * 0.45),
                        tp1=price + (atr * 2.0),
                        tp2=price + (atr * 3.5),
                        rationale=f"OB retracement. AMD={amd_score:.2f}{extra}, vol={vol_regime}",
                        veto_reason=veto,
                    ))

        elif ob.ob_type == "BEARISH":
            near_ob = abs(price - ob.low) < atr * 0.5 or (ob.low <= price <= ob.high)
            decent_session = ms.session.phase in ("LONDON_OPEN", "NY_OPEN", "NY_PM") or vol_regime == "high"
            decent_amd = amd_score < -0.15

            if near_ob and (decent_session or decent_amd):
                veto = has_veto("SELL")
                if not veto:
                    extra = " + manipulation" if ms.session.manipulation_detected else ""
                    score = 0.72 + (0.1 if ms.session.manipulation_detected else 0) + (0.08 if vol_regime == "high" else 0)
                    setups.append(Setup(
                        name="OB_RETRACEMENT",
                        direction="SELL",
                        score=min(0.92, score),
                        confluences=["BEARISH_OB", ms.session.phase, "STRUCTURE"],
                        entry_zone=(ob.low, ob.high),
                        stop_suggestion=ob.high + (atr * 0.45),
                        tp1=price - (atr * 2.0),
                        tp2=price - (atr * 3.5),
                        rationale=f"OB retracement. AMD={amd_score:.2f}{extra}, vol={vol_regime}",
                        veto_reason=veto,
                    ))

    # ------------------------------------------------------------------
    # SETUP 2: Liquidity Sweeps (still good quality)
    # ------------------------------------------------------------------
    if ms.session.manipulation_detected and ms.session.phase in ("LONDON_OPEN", "NY_OPEN"):
        for liq in ms.liquidity_levels:
            if liq.level_type == "EQUAL_HIGHS" and price >= liq.price - (atr * 0.12):
                veto = has_veto("SELL")
                if not veto and liq_score < -0.35:
                    setups.append(Setup(
                        name="LIQUIDITY_SWEEP",
                        direction="SELL",
                        score=0.79,
                        confluences=["EQUAL_HIGHS_SWEEP", "MANIPULATION"],
                        entry_zone=(liq.price - atr*0.1, liq.price + atr*0.12),
                        stop_suggestion=liq.price + (atr * 0.6),
                        tp1=price - (atr * 1.9),
                        tp2=price - (atr * 3.2),
                        rationale=f"Liquidity sweep at equal highs. LiqScore={liq_score:.2f}",
                        veto_reason=veto,
                    ))

            if liq.level_type == "EQUAL_LOWS" and price <= liq.price + (atr * 0.12):
                veto = has_veto("BUY")
                if not veto and liq_score > 0.35:
                    setups.append(Setup(
                        name="LIQUIDITY_SWEEP",
                        direction="BUY",
                        score=0.79,
                        confluences=["EQUAL_LOWS_SWEEP", "MANIPULATION"],
                        entry_zone=(liq.price - atr*0.12, liq.price + atr*0.1),
                        stop_suggestion=liq.price - (atr * 0.6),
                        tp1=price + (atr * 1.9),
                        tp2=price + (atr * 3.2),
                        rationale=f"Liquidity sweep at equal lows. LiqScore={liq_score:.2f}",
                        veto_reason=veto,
                    ))

    # ------------------------------------------------------------------
    # NEW SETUP 3: Trend Continuation (to avoid missing strong moves)
    # ------------------------------------------------------------------
    if ms.bias == "BULLISH" and total_confluence > 0.55:
        # Price has made higher highs/lows and is pulling back to minor structure
        has_bull_bos = any(b.break_type == "BOS" and b.direction == "BULL" for b in ms.breaks[-4:])
        minor_pullback = any(abs(price - ob.high) < atr * 0.6 for ob in ms.order_blocks if ob.ob_type == "BULLISH" and not ob.is_mitigated)

        if has_bull_bos and (minor_pullback or fib_score > 0.3):
            veto = has_veto("BUY")
            if not veto:
                setups.append(Setup(
                    name="TREND_CONTINUATION",
                    direction="BUY",
                    score=0.71,
                    confluences=["HTF_BIAS", "BOS", "STRUCTURE_PULLBACK"],
                    entry_zone=(price - atr*0.3, price + atr*0.3),
                    stop_suggestion=price - (atr * 0.9),
                    tp1=price + (atr * 1.8),
                    tp2=price + (atr * 3.0),
                    rationale=f"Trend continuation in bullish structure. Vol={vol_regime}",
                    veto_reason=veto,
                ))

    if ms.bias == "BEARISH" and total_confluence < -0.55:
        has_bear_bos = any(b.break_type == "BOS" and b.direction == "BEAR" for b in ms.breaks[-4:])
        minor_pullback = any(abs(price - ob.low) < atr * 0.6 for ob in ms.order_blocks if ob.ob_type == "BEARISH" and not ob.is_mitigated)

        if has_bear_bos and (minor_pullback or fib_score < -0.3):
            veto = has_veto("SELL")
            if not veto:
                setups.append(Setup(
                    name="TREND_CONTINUATION",
                    direction="SELL",
                    score=0.71,
                    confluences=["HTF_BIAS", "BOS", "STRUCTURE_PULLBACK"],
                    entry_zone=(price - atr*0.3, price + atr*0.3),
                    stop_suggestion=price + (atr * 0.9),
                    tp1=price - (atr * 1.8),
                    tp2=price - (atr * 3.0),
                    rationale=f"Trend continuation in bearish structure. Vol={vol_regime}",
                    veto_reason=veto,
                ))

    # ------------------------------------------------------------------
    # SETUP 3: Fibonacci at Structural Confluence (only when it matters)
    # ------------------------------------------------------------------
    if ms.bias != "NEUTRAL" and len(ms.order_blocks) > 0:
        # Simple projection using last two swings if available
        swing_highs = [s.price for s in ms.swings if s.swing_type == "HIGH"][-2:]
        swing_lows = [s.price for s in ms.swings if s.swing_type == "LOW"][-2:]

        if len(swing_highs) >= 2 and len(swing_lows) >= 1 and ms.bias == "BULLISH":
            leg = swing_highs[-1] - swing_lows[-1]
            fib_618 = swing_highs[-1] - leg * 0.618
            if abs(price - fib_618) < atr * 0.45:
                nearest_ob = _nearest_active_ob(ms, "BUY")
                if nearest_ob:
                    veto = has_veto("BUY")
                    if not veto:
                        setups.append(Setup(
                            name="FIB_STRUCTURAL_CONFLUENCE",
                            direction="BUY",
                            score=0.75,
                            confluences=["FIB_618", "BULLISH_OB", ms.bias],
                            entry_zone=(fib_618 - atr*0.2, fib_618 + atr*0.2),
                            stop_suggestion=nearest_ob.low - (atr * 0.3),
                            tp1=price + (atr * 2.4),
                            tp2=price + (atr * 4.0),
                            rationale=f"Price at 61.8% retracement into bullish order block. Strong structural confluence.",
                            veto_reason=veto,
                        ))

    # ------------------------------------------------------------------
    # BOOST & FILTER USING CONTEXTUAL SCORES
    # ------------------------------------------------------------------
    for setup in setups:
        boost = 0.0
        if setup.direction == "BUY":
            boost = (max(0, amd_score) * 0.35) + (max(0, fib_score) * 0.30) + (max(0, pa_score) * 0.20) + (max(0, liq_score) * 0.25)
        else:
            boost = (abs(min(0, amd_score)) * 0.35) + (abs(min(0, fib_score)) * 0.30) + (abs(min(0, pa_score)) * 0.20) + (abs(min(0, liq_score)) * 0.25)

        setup.score = min(0.98, setup.score + boost * 0.6)

        # Add the contextual sources to the confluences list
        extra = []
        if abs(amd_score) > 0.35: extra.append("AMD_SESSION")
        if abs(fib_score) > 0.35: extra.append("FIB_STRUCTURAL")
        if abs(pa_score) > 0.35: extra.append("PA_AT_STRUCTURE")
        if abs(liq_score) > 0.35: extra.append("LIQUIDITY_SWEEP")
        if extra:
            setup.confluences = list(set(setup.confluences + extra))

    # ------------------------------------------------------------------
    # FINAL FILTER (balanced for frequency + quality)
    # ------------------------------------------------------------------
    valid = [s for s in setups if not s.veto_reason]

    # Keep decent conviction setups
    valid = [s for s in valid if s.score >= 0.65]

    # Prefer higher conviction
    valid.sort(key=lambda s: -s.score)

    # Allow up to 3 competing ideas (more opportunities without being reckless)
    return valid[:3]


def get_structure_signal(ms: MarketStructure, spread: float = 0.0) -> Dict:
    """
    Main public API for the new engine. Returns a rich, explainable signal.
    """
    setups = evaluate_setups(ms, spread)

    if not setups:
        return {
            "signal": "HOLD",
            "score": 0.0,
            "confidence": 0.0,
            "engine": "structure_v2_strict",
            "setup": None,
            "confluences": [],
            "rationale": "No high-quality setups after strict filters (post-backtest tuning).",
            "session": ms.session.phase,
            "bias": ms.bias,
            "market_structure": ms.to_dict(),
        }

    best = setups[0]

    # Map internal score to the old -1..1 range for partial compatibility
    mapped_score = best.score * (1.0 if best.direction == "BUY" else -1.0)

    return {
        "signal": best.direction,
        "score": round(mapped_score, 4),
        "confidence": round(best.score * 100, 1),
        "engine": "structure_v2_strict",
        "setup": best.name,
        "confluences": best.confluences,
        "rationale": best.rationale,
        "entry_zone": [round(x, 5) for x in best.entry_zone],
        "stop_suggestion": round(best.stop_suggestion, 5),
        "tp1": round(best.tp1, 5),
        "tp2": round(best.tp2, 5),
        "session": ms.session.phase,
        "bias": ms.bias,
        "contextual_scores": {
            "amd": round(amd_score, 3),
            "fib_confluence": round(fib_score, 3),
            "price_action": round(pa_score, 3),
            "liquidity": round(liq_score, 3),
            "total": round(total_confluence, 3),
        },
        "market_structure": ms.to_dict(),
        "all_setups": [
            {"name": s.name, "direction": s.direction, "score": round(s.score, 2)} for s in setups
        ],
    }

    # Log every signal for future analysis (critical for your aggressive goal)
    try:
        log_signal(result)
    except Exception:
        pass

    return result
