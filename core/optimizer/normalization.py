"""
core/optimizer/normalization.py
===============================
Text normalization: accent removal, column name matching, fix_text.
"""
from __future__ import annotations

import logging
import re
import unicodedata

_log = logging.getLogger("ferment.optimizer.normalization")


def _norm_colname(s: str) -> str:
    s = str(s or "")
    s = s.strip().lower()
    # enleve accents
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))
    # remplace tout le reste par des espaces
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _pick_column(df, candidates_norm: list[str]) -> str | None:
    """
    Retourne le vrai nom de colonne du df correspondant a des candidats "normalises".
    Ameliore : accepte 'produit 1', 'produit_2', etc. + correspondances partielles.
    """
    norm_to_real = {_norm_colname(c): c for c in df.columns}
    norms = list(norm_to_real.keys())

    # 1) match exact (priorite)
    for cand in candidates_norm:
        if cand in norm_to_real:
            return norm_to_real[cand]

    # 2) startswith sur les mots-cles importants
    KEY_PREFIXES = ["produit", "designation", "desigation", "des", "libelle", "product", "item", "sku"]
    for key in KEY_PREFIXES:
        for n in norms:
            if n.startswith(key):
                return norm_to_real[n]

    # 3) contains
    for key in KEY_PREFIXES:
        for n in norms:
            if key in n:
                return norm_to_real[n]

    # 4) fuzzy (secours)
    try:
        import difflib
        match = difflib.get_close_matches(candidates_norm[0], norms, n=1, cutoff=0.85)
        if match:
            return norm_to_real[match[0]]
    except (ImportError, ValueError, TypeError):
        _log.debug(
            "Colonne %s non trouvee dans les colonnes disponibles",
            candidates_norm[0] if candidates_norm else "?",
        )
    return None


# ======= util accents (fix_text) =============================================
ACCENT_CHARS = "éèêëàâäîïôöùûüçÉÈÊËÀÂÄÎÏÔÖÙÛÜÇ"

CUSTOM_REPLACEMENTS = {
    "M\uFFFDlisse": "Mélisse",
    "poivr\uFFFDe": "poivrée",
    "P\uFFFDche": "Pêche",
}


def _looks_better(a: str, b: str) -> bool:
    def score(s):
        return sum(ch in ACCENT_CHARS for ch in s)
    return score(b) > score(a)


def fix_text(s) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    s0 = s
    try:
        s1 = s0.encode("latin1").decode("utf-8")
        if _looks_better(s0, s1):
            s0 = s1
    except (UnicodeDecodeError, UnicodeEncodeError):
        _log.debug("Erreur normalisation texte: %r", s0, exc_info=True)
    if s0 in CUSTOM_REPLACEMENTS:
        return CUSTOM_REPLACEMENTS[s0]
    if "\uFFFD" in s0:
        s0 = s0.replace("\uFFFD", "é")
    return s0
