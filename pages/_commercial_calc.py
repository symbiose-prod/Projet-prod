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
    """Extrait les coordonnées depuis un ModeleIndicateurResultat.

    EasyBeer retourne des données **journalières** :
    - ``series[0].values`` = liste de ``{label: None, x: "DD/MM/YYYY", y: montant}``
    """
    series = data.get("series") or []
    if not series and data.get("serie"):
        series = [data["serie"]]

    if not series:
        return []

    values = series[0].get("values") or series[0].get("data") or []
    return values


def _build_month_map(values: list[dict]) -> dict[int, float]:
    """Agrège les données journalières par mois → {mois: CA total}.

    Le champ ``x`` contient la date au format ``DD/MM/YYYY``.
    Le champ ``y`` contient le CA du jour.
    """
    result: dict[int, float] = {}
    for v in values:
        y = v.get("y")
        if y is None:
            continue
        amount = float(y)
        if amount == 0:
            continue

        x = v.get("x") or v.get("label") or ""
        month = _extract_month(str(x))
        if month is not None:
            result[month] = result.get(month, 0.0) + amount

    return result


def _extract_month(date_str: str) -> int | None:
    """Extrait le mois (1-12) depuis une date DD/MM/YYYY ou YYYY-MM-DD."""
    import re

    if not date_str:
        return None

    # DD/MM/YYYY
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_str)
    if m:
        return int(m.group(2))

    # YYYY-MM-DD
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_str)
    if m:
        return int(m.group(2))

    return None


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
