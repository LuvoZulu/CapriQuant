"""
app/strategies/__init__.py
==========================
Integration fix: wire crt_strategy and session_amd into the package.

CHANGES FROM ORIGINAL
---------------------
- Added: crt_strategy (CRTStrategy class — the full candle-range module)
- Added: session_amd  (SessionAMDDetector — adaptive vol-aware AMD)
- Both were written but never imported here, making them dead code.
"""

# Contextual structure-first strategies (existing)
from . import structure, amd, fibonacci, price_action, liquidity, crt

# Newly wired modules
from . import crt_strategy   # CRTStrategy: range levels, CE, expansion targets
from . import session_amd    # SessionAMDDetector: vol-gated AMD phase classification

__all__ = [
    "structure",
    "amd",
    "fibonacci",
    "price_action",
    "liquidity",
    "crt",
    "crt_strategy",
    "session_amd",
]