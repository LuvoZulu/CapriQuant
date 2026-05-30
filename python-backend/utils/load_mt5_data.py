"""
Robust loader for MT5 exported CSV files (History Center format).

MT5 exports often come with weird headers like:
<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>

This module handles them reliably.
"""

import pandas as pd
from pathlib import Path
from typing import Optional


def load_mt5_csv(filepath: str | Path, symbol: Optional[str] = None) -> pd.DataFrame:
    """
    Load an MT5 exported CSV (tab separated with < > headers).
    Returns a clean DataFrame with columns:
    timestamp, open, high, low, close, volume
    """
    path = Path(filepath)

    # Read as tab-separated, skip the first header line if it contains <DATE>
    df = pd.read_csv(
        path,
        sep="\t",
        header=0,
        engine="python"
    )

    # Clean column names (remove < > if present)
    df.columns = [c.replace("<", "").replace(">", "").strip().upper() for c in df.columns]

    # Rename to our standard
    rename_map = {
        "DATE": "date",
        "TIME": "time",
        "OPEN": "open",
        "HIGH": "high",
        "LOW": "low",
        "CLOSE": "close",
        "TICKVOL": "volume",
        "VOL": "volume_real",
        "SPREAD": "spread"
    }
    df = df.rename(columns=rename_map)

    # Combine date + time into proper timestamp
    if "date" in df.columns and "time" in df.columns:
        df["timestamp"] = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str), errors="coerce")
    elif "DATE" in df.columns:   # fallback
        df["timestamp"] = pd.to_datetime(df["DATE"], errors="coerce")
    else:
        raise ValueError("Could not find date/time columns in MT5 export")

    df = df.dropna(subset=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Keep only what we need
    keep_cols = ["timestamp", "open", "high", "low", "close", "volume"]
    available = [c for c in keep_cols if c in df.columns]
    df = df[available]

    # Infer symbol from filename if not provided
    if symbol is None:
        name = path.stem.upper()
        if "XAU" in name:
            symbol = "XAUUSD"
        elif "US30" in name:
            symbol = "US30"
        elif "USTEC" in name or "NAS" in name:
            symbol = "NAS100"
        elif "DE30" in name or "GER" in name or "DAX" in name:
            symbol = "GER30"
        else:
            symbol = path.stem.split("_")[0]

    df["symbol"] = symbol
    return df


def load_all_testing_data(testing_dir: str = "testing") -> dict:
    """
    Automatically load all CSVs from the testing folder and return
    a dict keyed by (symbol, timeframe).
    """
    testing_path = Path(testing_dir)
    files = list(testing_path.glob("*.csv"))

    data = {}
    for f in files:
        try:
            df = load_mt5_csv(f)
            tf = "M1" if "_M1_" in f.name else "M5" if "_M5_" in f.name else "M15" if "_M15_" in f.name else "UNKNOWN"
            key = (df["symbol"].iloc[0], tf)
            data[key] = df
            print(f"Loaded: {f.name} -> {key[0]} {key[1]} ({len(df)} bars)")
        except Exception as e:
            print(f"Failed to load {f.name}: {e}")

    return data
