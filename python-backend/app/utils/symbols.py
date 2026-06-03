"""Canonical symbol normalization for buffers, DB, and UI."""

from __future__ import annotations
from typing import List, Optional


def normalize_symbol(symbol: str) -> str:
    if not symbol:
        return symbol
    s = str(symbol).upper().strip()

    if s in ("GOLD",) or s.startswith("XAU"):
        return "XAUUSD"
    if any(x in s for x in ("GER30", "DE30", "DAX", "GDAXI")):
        return "DE30"
    if any(x in s for x in ("USTEC", "NAS100", "NDX", "US100")):
        return "USTEC"
    if any(x in s for x in ("US30", "DJ30", "DOW")):
        return "US30"

    suffixes = [".PRO", ".STD", ".ECN", ".RAW", "#", "M", "PRO", "STD"]
    for suf in sorted(suffixes, key=len, reverse=True):
        if s.endswith(suf) and len(s) > len(suf) + 2:
            s = s[: -len(suf)]
            break

    if s in ("GOLD",) or s.startswith("XAU"):
        return "XAUUSD"
    if any(x in s for x in ("GER30", "DE30", "DAX")):
        return "DE30"
    if any(x in s for x in ("USTEC", "NAS100", "NDX")):
        return "USTEC"

    return s


def symbol_variants(canonical: str) -> List[str]:
    """All broker keys that should map to one canonical symbol."""
    c = normalize_symbol(canonical)
    variants = {c, c + "M", c + "#", c + ".PRO"}
    if c == "XAUUSD":
        variants.update({"XAUUSD", "XAUUSDM", "XAUUSD.", "GOLD", "GOLDM", "XAUUSD#"})
    elif c == "DE30":
        variants.update({"DE30", "DE30M", "GER30", "GER30M", "DAX"})
    elif c == "USTEC":
        variants.update({"USTEC", "USTECM", "NAS100", "US100"})
    elif c == "US30":
        variants.update({"US30", "US30M", "DJ30"})
    return sorted(variants)


def symbol_sql_match(canonical: str) -> tuple:
    """
    Returns (sql_fragment, params) for WHERE clause matching symbol family.
    """
    variants = symbol_variants(canonical)
    placeholders = ", ".join(["%s"] * len(variants))
    return f"symbol IN ({placeholders})", tuple(variants)


# Dashboard / monitoring scope (no OIL)
MONITORED_SYMBOLS = ("XAUUSD", "DE30", "USTEC", "US30")


def is_monitored(symbol: str) -> bool:
    return normalize_symbol(symbol) in MONITORED_SYMBOLS
