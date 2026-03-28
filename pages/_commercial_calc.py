"""
pages/_commercial_calc.py
=========================
Calculs pour le dashboard commercial — fonctions pures, thread-safe.
"""
from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("ferment.commercial")


def _parse_ca_series(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extrait les coordonnées (label, montant) depuis un ModeleIndicateurResultat.

    EasyBeer retourne soit ``series`` (liste) soit ``serie`` (objet unique).
    Chaque série a un champ ``values`` contenant des ``{label, x, y}``.
    """
    series = data.get("series") or []
    if not series and data.get("serie"):
        series = [data["serie"]]

    # Prendre la première série (CA HT principal)
    if not series:
        return []

    values = series[0].get("values") or series[0].get("data") or []
    return values


def _month_index_from_label(label: str) -> int | None:
    """Extrait le numéro de mois (1-12) depuis un label comme 'Janvier 2025' ou '01/2025'."""
    if not label:
        return None

    label = label.strip().lower()

    # Format "Janvier 2025", "Février 2025", etc.
    _MOIS = {
        "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    }
    for nom, num in _MOIS.items():
        if nom in label:
            return num

    # Format "01/2025" ou "2025-01"
    import re
    m = re.match(r"(\d{1,2})[/\-](\d{4})", label)
    if m:
        return int(m.group(1))
    m = re.match(r"(\d{4})[/\-](\d{1,2})", label)
    if m:
        return int(m.group(2))

    return None


def _build_month_map(values: list[dict]) -> dict[int, float]:
    """Convertit une liste de coordonnées en {mois: montant}."""
    result: dict[int, float] = {}
    for v in values:
        label = v.get("label", "")
        y = v.get("y")
        if y is None:
            continue
        month = _month_index_from_label(label)
        if month is not None:
            result[month] = float(y)
    return result


def fetch_ca_comparison(
    year_a: int = 2025,
    year_b: int = 2026,
) -> dict[str, Any]:
    """Fetch CA pour deux années et construit le comparatif mensuel.

    Retourne::

        {
            "year_a": 2025,
            "year_b": 2026,
            "months": [
                {"month": 1, "label": "Janvier", "ca_a": 12345.0, "ca_b": 15000.0, "pct": 21.5},
                ...
            ],
            "ytd_a": 45000.0,
            "ytd_b": 52000.0,
            "ytd_pct": 15.6,
            "current_month": 3,
        }
    """
    import datetime

    from common.easybeer.indicators import get_chiffre_affaire

    now = datetime.datetime.now(datetime.UTC)
    current_month = now.month

    # Appel CA année A (complète)
    _log.info("Fetching CA %d...", year_a)
    data_a = get_chiffre_affaire(
        date_debut=f"{year_a}-01-01T00:00:00.000Z",
        date_fin=f"{year_a}-12-31T23:59:59.999Z",
    )
    ca_a = _build_month_map(_parse_ca_series(data_a))

    # Appel CA année B (jusqu'à aujourd'hui)
    _log.info("Fetching CA %d...", year_b)
    data_b = get_chiffre_affaire(
        date_debut=f"{year_b}-01-01T00:00:00.000Z",
        date_fin=f"{year_b}-12-31T23:59:59.999Z",
    )
    ca_b = _build_month_map(_parse_ca_series(data_b))

    # Construire le tableau mensuel
    _MOIS_LABELS = [
        "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
        "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
    ]

    months = []
    ytd_a = 0.0
    ytd_b = 0.0

    for m in range(1, 13):
        a = ca_a.get(m, 0.0)
        b = ca_b.get(m, 0.0)

        if a > 0:
            pct = round((b - a) / a * 100, 1)
        elif b > 0:
            pct = 100.0  # de 0 à quelque chose = +100%
        else:
            pct = 0.0

        months.append({
            "month": m,
            "label": _MOIS_LABELS[m],
            "ca_a": round(a, 2),
            "ca_b": round(b, 2),
            "pct": pct,
        })

        # Cumul YTD jusqu'au mois courant (de l'année B)
        if m <= current_month:
            ytd_a += a
            ytd_b += b

    ytd_pct = round((ytd_b - ytd_a) / ytd_a * 100, 1) if ytd_a > 0 else (100.0 if ytd_b > 0 else 0.0)

    return {
        "year_a": year_a,
        "year_b": year_b,
        "months": months,
        "ytd_a": round(ytd_a, 2),
        "ytd_b": round(ytd_b, 2),
        "ytd_pct": ytd_pct,
        "current_month": current_month,
    }
