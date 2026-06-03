"""
Central configuration for CapriQuant.

Loads from environment with sensible defaults.
Used for risk params, thresholds, symbols, costs, etc.
Easy to override without code changes.

Example .env:
CAPRI_RISK_MAX_DAILY_LOSS_PCT=5.0
CAPRI_MIN_CONFLUENCE=0.48
CAPRI_SYMBOLS=XAUUSD,US30,USTEC,DE30
"""

import os
from dataclasses import dataclass, field
from typing import List

@dataclass
class Settings:
    # Risk
    risk_max_per_trade_pct: float = 2.5
    risk_max_daily_loss_pct: float = 6.0
    risk_base_pct: float = 1.2
    risk_aggressive_pct: float = 2.0
    risk_conservative_pct: float = 0.7
    risk_starting_equity: float = 200.0
    risk_target_equity: float = 17000.0

    # Confluence / signal
    min_confluence_for_setup: float = 0.48
    min_confidence_pct: float = 68.0
    mtf_min_confidence: float = 78.0
    mtf_min_m5_confluence: float = 0.82

    # Symbols
    symbols: List[str] = field(default_factory=lambda: ["XAUUSD", "US30", "USTEC", "DE30"])

    # Costs for backtest / risk awareness
    default_spread_points: float = 0.30
    commission_r_per_trade: float = 0.02

    # Management
    management_enabled: bool = True
    be_pad_atr: float = 0.15
    trail_pad_atr: float = 0.3

    # Data quality
    max_spread_for_trade: float = 400.0  # points

    # System
    backend_port: int = 8001
    poll_seconds: int = 2

    def __post_init__(self):
        # Override from env
        self.risk_max_daily_loss_pct = float(os.getenv("CAPRI_RISK_MAX_DAILY_LOSS_PCT", self.risk_max_daily_loss_pct))
        self.risk_base_pct = float(os.getenv("CAPRI_RISK_BASE_PCT", self.risk_base_pct))
        self.min_confluence_for_setup = float(os.getenv("CAPRI_MIN_CONFLUENCE", self.min_confluence_for_setup))
        syms = os.getenv("CAPRI_SYMBOLS")
        if syms:
            self.symbols = [s.strip().upper() for s in syms.split(",") if s.strip()]
        self.default_spread_points = float(os.getenv("CAPRI_SPREAD_POINTS", self.default_spread_points))
        self.management_enabled = os.getenv("CAPRI_MANAGEMENT_ENABLED", "true").lower() == "true"

# Global instance
settings = Settings()

def get_settings() -> Settings:
    return settings
