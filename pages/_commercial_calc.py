"""
pages/_commercial_calc.py
=========================
Calculs pour le dashboard commercial — fonctions pures, thread-safe.

Logique de prévision :
- Taux de croissance = moyenne des 2 derniers mois complets (CA_2026/CA_2025 - 1)
- Prévision mois futur = CA_2025[m] × (1 + taux)
- Mois en cours = réalisé + (prévision_totale - réalisé) comme part prévisionnelle
- CA cible fin d'année = mois réalisés + prévisions mois restants
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

_log = logging.getLogger("ferment.commercial")

_MOIS_LABELS = [
    "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]

_MOIS_MAP = {
    "janv.": 1, "févr.": 2, "mars": 3, "avr.": 4, "mai": 5, "juin": 6,
    "juil.": 7, "août": 8, "sept.": 9, "oct.": 10, "nov.": 11, "déc.": 12,
    # fallbacks
    "janvier": 1, "février": 2, "avril": 4, "juillet": 7,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
}


def _parse_monthly_series(data: dict[str, Any]) -> tuple[dict[int, float], dict[int, float]]:
    """Parse le résultat de get_ca_mensuel() → (ca_current, ca_reference).

    EasyBeer retourne 2 séries mensuelles :
    - series[0] : année courante (ex: "janv. 2026" → y)
    - series[1] : période de référence (ex: "janv. 2025" → y)

    Retourne 2 dicts {mois(1-12): montant}.
    """
    series = data.get("series") or []
    ca_current: dict[int, float] = {}
    ca_ref: dict[int, float] = {}

    for idx, serie in enumerate(series[:2]):
        values = serie.get("values") or serie.get("data") or []
        target = ca_current if idx == 0 else ca_ref
        for v in values:
            x = str(v.get("x") or "").strip().lower()
            y = v.get("y")
            if y is None:
                continue
            month = _extract_month_from_label(x)
            if month is not None:
                target[month] = float(y)

    return ca_current, ca_ref


def _extract_month_from_label(label: str) -> int | None:
    """Extrait le mois depuis 'janv. 2026', 'févr. 2025', etc."""
    if not label:
        return None
    # Prendre le premier mot (avant l'espace + année)
    parts = label.split()
    if not parts:
        return None
    month_word = parts[0].rstrip(".")
    # Chercher dans le mapping
    for key, num in _MOIS_MAP.items():
        if key.startswith(month_word) or month_word.startswith(key.rstrip(".")):
            return num
    return None


def _compute_growth_rate(
    ca_a: dict[int, float],
    ca_b: dict[int, float],
    current_month: int,
) -> float:
    """Calcule le taux moyen d'évolution sur les 2 derniers mois glissants.

    Prend les 2 derniers mois (incluant le mois en cours) où ca_b > 0 ET ca_a > 0.
    Ex: si on est en mars, prend mars et février.
    Retourne le taux moyen (ex: 0.15 pour +15%).
    """
    rates: list[float] = []
    # Parcourir depuis le mois en cours vers le passé
    for m in range(current_month, 0, -1):
        a = ca_a.get(m, 0)
        b = ca_b.get(m, 0)
        if a > 0 and b > 0:
            rates.append(b / a - 1)
        if len(rates) >= 2:
            break

    if not rates:
        return 0.0
    return sum(rates) / len(rates)


def fetch_ca_comparison(
    year_a: int = 2025,
    year_b: int = 2026,
) -> dict[str, Any]:
    """Fetch CA mensuel 2025 vs 2026 avec prévisions.

    Retourne::

        {
            "year_a": 2025, "year_b": 2026,
            "current_month": 3, "current_day": 28,
            "months": [
                {
                    "month": 1, "label": "Janvier",
                    "ca_a": 64501.0, "ca_b": 75775.0,
                    "forecast": 0.0,            # 0 si mois passé
                    "ca_b_realized": 75775.0,    # = ca_b pour mois passés
                    "pct": 17.5,
                },
                ...
                {   # Mois en cours (ex: mars)
                    "month": 3, "ca_a": 100274.0,
                    "ca_b": 164095.0,            # réalisé à date
                    "forecast": 5000.0,           # estimation restante
                    "ca_b_realized": 164095.0,
                    "pct": 12.3,
                },
                {   # Mois futur
                    "month": 4, "ca_a": 80000.0,
                    "ca_b": 0.0,
                    "forecast": 92000.0,          # CA_2025 × (1 + taux)
                    "ca_b_realized": 0.0,
                    "pct": 15.0,                  # taux prévisionnel
                },
            ],
            "ytd_a": ..., "ytd_b": ..., "ytd_pct": ...,
            "ca_cible": ...,  # CA cible fin d'année
            "growth_rate": 0.15,  # taux 2 derniers mois
        }
    """
    from common.easybeer.indicators import get_ca_mensuel

    now = datetime.datetime.now(datetime.UTC)
    current_month = now.month
    current_day = now.day

    # ── Appel API : CA mensuel année B avec référence année A ──
    _log.info("Fetching CA mensuel %d (avec référence %d)...", year_b, year_a)
    data = get_ca_mensuel(year_b, include_avoir=True, include_carnet=True)
    ca_b, ca_a = _parse_monthly_series(data)

    _log.info(
        "CA parsé: %d mois année %d, %d mois année %d",
        len(ca_b), year_b, len(ca_a), year_a,
    )

    # ── Taux de croissance (2 derniers mois complets) ──
    growth_rate = _compute_growth_rate(ca_a, ca_b, current_month)
    _log.info("Taux de croissance (2 derniers mois): %.1f%%", growth_rate * 100)

    # ── Construire le tableau mensuel ──
    months: list[dict[str, Any]] = []
    ytd_a = 0.0
    ytd_b = 0.0
    ca_cible = 0.0

    for m in range(1, 13):
        a = ca_a.get(m, 0.0)
        b = ca_b.get(m, 0.0)

        if m < current_month:
            # Mois passé : tout est réalisé
            forecast = 0.0
            realized = b
            ca_cible += b
        elif m == current_month:
            # Mois en cours : réalisé + estimation du reste
            realized = b
            forecast_total = a * (1 + growth_rate) if a > 0 else b
            forecast = max(0.0, forecast_total - b)
            ca_cible += b + forecast
        else:
            # Mois futur : tout est prévisionnel
            realized = 0.0
            forecast = a * (1 + growth_rate) if a > 0 else 0.0
            ca_cible += forecast

        # % évolution
        if m <= current_month:
            # Mois avec données réelles
            pct = round((b - a) / a * 100, 1) if a > 0 else (100.0 if b > 0 else 0.0)
        else:
            # Mois futur : taux prévisionnel
            pct = round(growth_rate * 100, 1)

        months.append({
            "month": m,
            "label": _MOIS_LABELS[m],
            "ca_a": round(a, 2),
            "ca_b": round(b, 2),
            "ca_b_realized": round(realized, 2),
            "forecast": round(forecast, 2),
            "pct": pct,
        })

        # Cumul YTD (à date du jour, pas au mois complet)
        if m < current_month:
            ytd_a += a
            ytd_b += b
        elif m == current_month:
            # Prorata du mois en cours pour year_a
            import calendar
            days_in_month = calendar.monthrange(year_a, m)[1]
            ratio = current_day / days_in_month
            ytd_a += a * ratio
            ytd_b += b  # b est déjà le réalisé à date

    ytd_pct = round((ytd_b - ytd_a) / ytd_a * 100, 1) if ytd_a > 0 else (100.0 if ytd_b > 0 else 0.0)

    return {
        "year_a": year_a,
        "year_b": year_b,
        "current_month": current_month,
        "current_day": current_day,
        "months": months,
        "ytd_a": round(ytd_a, 2),
        "ytd_b": round(ytd_b, 2),
        "ytd_pct": ytd_pct,
        "ca_cible": round(ca_cible, 2),
        "growth_rate": round(growth_rate * 100, 1),
    }
