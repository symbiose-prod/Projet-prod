"""
common/session_store.py
========================
Compressed session storage helpers.

Stores DataFrames as zlib-compressed JSON strings (base64-encoded).
Typical compression ratio: 80-90% (3 MB → 300 KB).

Backward-compatible: load_df() auto-detects raw JSON vs compressed.
"""
from __future__ import annotations

import base64
import io
import logging
import zlib

import pandas as pd

_log = logging.getLogger("ferment.session_store")

_MAGIC = "zlib:"  # prefix to distinguish compressed from raw JSON


def store_df(df: pd.DataFrame) -> str:
    """Serialize a DataFrame to a compressed string for session storage."""
    raw_json = df.to_json(orient="split")
    compressed = zlib.compress(raw_json.encode("utf-8"), level=6)
    encoded = base64.b64encode(compressed).decode("ascii")
    result = f"{_MAGIC}{encoded}"
    ratio = len(result) / max(len(raw_json), 1) * 100
    _log.debug(
        "store_df: %d bytes -> %d bytes (%.0f%% of original)",
        len(raw_json), len(result), ratio,
    )
    return result


def load_df(stored: str) -> pd.DataFrame:
    """Deserialize a DataFrame from session storage (handles both compressed and raw)."""
    if not stored:
        return pd.DataFrame()
    if stored.startswith(_MAGIC):
        encoded = stored[len(_MAGIC):]
        compressed = base64.b64decode(encoded)
        raw_json = zlib.decompress(compressed).decode("utf-8")
    else:
        # Backward compatibility: raw JSON (not compressed)
        raw_json = stored
    return pd.read_json(io.StringIO(raw_json), orient="split")
