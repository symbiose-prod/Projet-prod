"""
common/xlsx_fill/_helpers.py
============================
Shared constants, normalization, path utilities.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path


# ======= Normalisation & mapping gouts =======================================

def _norm_key(s: str) -> str:
    s = str(s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("\u2019", "'")
    s = re.sub(r"[\s\-_/]+", " ", s)
    return " ".join(s.split())


# Canonical -> libelle EXACT attendu par Excel
EXCEL_LABEL_MAP = {
    _norm_key("Original"):           "K. Original",
    _norm_key("Menthe citron vert"): "K. Menthe - Citron Vert",
    _norm_key("Gingembre"):          "K. Gingembre",
    _norm_key("Pamplemousse"):       "K. Pamplemousse",
    _norm_key("Mangue Passion"):     "K. Mangue - Passion",
    _norm_key("Menthe Poivree"):     "EP. Menthe Poivr\u00e9e",
    _norm_key("M\u00e9lisse"):       "EP. M\u00e9lisse",
    _norm_key("Anis \u00e9toil\u00e9e"): "EP. Anis \u00e9toil\u00e9e",
    _norm_key("Zeste d'agrumes"):    "EP. Zest d'agrumes",
    _norm_key("P\u00eache"):         "K. P\u00eache",
    _norm_key("Autre"):              "Autre :",
}


def _to_excel_label(gout: str) -> str:
    return EXCEL_LABEL_MAP.get(_norm_key(gout), str(gout or ""))


# ======= Utilitaires de chemin/asset =========================================

def _project_root() -> Path:
    """Racine du projet (= dossier parent de 'common')."""
    try:
        return Path(__file__).resolve().parents[2]
    except Exception:
        return Path(os.getcwd())


def _load_asset_bytes(rel_path: str) -> bytes | None:
    """Charge un fichier d'assets en bytes, peu importe le cwd."""
    root = _project_root()
    candidates = [root / rel_path, Path(rel_path)]
    for p in candidates:
        try:
            if p.exists() and p.is_file():
                return p.read_bytes()
        except Exception:
            pass
    return None


# ======= Constantes generales ================================================

VOL_TOL = 0.02
FILTRE_RATIO_KEFIR = 0.60  # proportion filtree pour le kefir (pas les infusions)


def _is_close(a: float, b: float, tol: float = VOL_TOL) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except Exception:
        return False
