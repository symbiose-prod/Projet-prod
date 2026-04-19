"""
common/session_store.py
========================
Compressed session storage helpers.

Stores DataFrames as zlib-compressed JSON strings (base64-encoded).
Typical compression ratio: 80-90% (3 MB → 300 KB).

Backward-compatible: load_df() auto-detects raw JSON vs compressed.

Expose aussi ``get_imported_df`` : accesseur du DataFrame importé par la page
Accueil (``app.storage.user["accueil"]["df_json"]``), consommé par Production,
Stocks et Commercial. L'import de ``nicegui.app`` est fait lazy (seulement si
get_imported_df est appelée) pour que le reste du module reste utilisable
dans des contextes hors NiceGUI (scripts, tests).
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


_MAX_DECOMPRESS_SIZE = 50_000_000  # 50 MB — protection anti zip-bomb


def load_df(stored: str) -> pd.DataFrame:
    """Deserialize a DataFrame from session storage (handles both compressed and raw)."""
    if not stored:
        return pd.DataFrame()
    if stored.startswith(_MAGIC):
        encoded = stored[len(_MAGIC):]
        compressed = base64.b64decode(encoded)
        # Décompression avec limite de taille (protection zip-bomb)
        decompressor = zlib.decompressobj()
        raw_bytes = decompressor.decompress(compressed, _MAX_DECOMPRESS_SIZE)
        if decompressor.unconsumed_tail:
            raise ValueError(
                f"Données compressées trop volumineuses (> {_MAX_DECOMPRESS_SIZE // 1_000_000} MB)"
            )
        raw_json = raw_bytes.decode("utf-8")
    else:
        # Backward compatibility: raw JSON (not compressed)
        raw_json = stored
    return pd.read_json(io.StringIO(raw_json), orient="split")


def get_imported_df() -> tuple[pd.DataFrame | None, int]:
    """Retourne le DataFrame importé (via Accueil) + la fenêtre d'analyse (jours).

    Import lazy de ``nicegui.app`` — les autres fonctions du module restent
    utilisables hors contexte NiceGUI. Retourne ``(None, 0)`` si aucun import
    n'a encore eu lieu (source de vérité : ``app.storage.user["accueil"]``).
    """
    try:
        from nicegui import app
    except ImportError:
        return None, 0
    try:
        state = app.storage.user.get("accueil", {}) or {}
    except Exception:
        return None, 0
    raw_json = state.get("df_json")
    if not raw_json:
        return None, 0
    try:
        df = load_df(raw_json)
    except Exception:
        _log.warning("Échec désérialisation df importé", exc_info=True)
        return None, 0
    return df, int(state.get("window_days", 30) or 30)
