"""
Unit tests for the RiskManager (core capital protection).
These must pass for any change to risk rules.
"""

from app.risk import RiskManager, RiskParams


def test_risk_manager_basic_sizing_and_streak_penalty():
    params = RiskParams(account_equity=500.0, base_risk_pct=1.2, aggressive_risk_pct=2.0, conservative_risk_pct=0.7)
    rm = RiskManager(params)
    # early progress -> aggressive
    r0 = rm.current_risk_pct(recent_losses=0)
    assert 1.5 < r0 <= 2.4
    # after losses, penalty reduces
    r2 = rm.current_risk_pct(recent_losses=2)
    assert r2 < r0
    assert r2 >= 0.35


def test_risk_manager_hard_daily_circuit():
    params = RiskParams(account_equity=1000.0, max_daily_loss_pct=5.0)
    rm = RiskManager(params)
    # today loss of 6% of start -> veto
    allowed, reason, rp = rm.can_take_trade(recent_loss_streak=0, today_pnl=-60.0, starting_equity_today=1000.0)
    assert allowed is False
    assert "DAILY_LOSS" in reason
    assert rp == 0.0


def test_risk_manager_hard_streak_circuit():
    params = RiskParams(account_equity=300.0)
    rm = RiskManager(params)
    allowed, reason, rp = rm.can_take_trade(recent_loss_streak=5, today_pnl=0.0, starting_equity_today=300.0)
    assert allowed is False
    assert "STREAK" in reason.upper()
    assert rp == 0.0


def test_risk_manager_allows_normal():
    params = RiskParams(account_equity=400.0, max_daily_loss_pct=6.0)
    rm = RiskManager(params)
    allowed, reason, rp = rm.can_take_trade(recent_loss_streak=1, today_pnl=-10.0, starting_equity_today=400.0)
    assert allowed is True
    assert reason == ""
    assert rp > 0.3


def test_calculate_trade_risk_smoke():
    params = RiskParams(account_equity=1000.0)
    rm = RiskManager(params)
    tr = rm.calculate_trade_risk(
        symbol="XAUUSD", direction="BUY", entry=2650.0, stop=2645.0, atr=5.0, recent_loss_streak=0
    )
    assert tr.risk_pct > 0
    assert tr.position_size > 0
    assert tr.risk_amount > 0


def test_validate_structure_stop_pads_tight_stops():
    params = RiskParams(account_equity=200.0)
    rm = RiskManager(params)
    # too tight stop -> moves it out to ~0.65 ATR
    padded = rm.validate_structure_stop(
        proposed_stop=100.0, current_price=100.5, nearest_ob_low=99.0, nearest_ob_high=101.0, atr=2.0
    )
    assert padded < 100.5  # moved lower for long
    assert abs(padded - 100.5) >= 1.2  # at least some pad
