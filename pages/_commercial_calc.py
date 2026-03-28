"""
pages/_commercial_calc.py
=========================
Calculs pour le dashboard commercial — fonctions pures, thread-safe.

Logique de prévision (2 mois glissants) :
- Fenêtre = [aujourd'hui - 2 mois] → [aujourd'hui]
- Taux = CA_2026(fenêtre) / CA_2025(même fenêtre) - 1
- Mois en cours : réalisé + prévision du reste (CA_2025 jours restants × (1+taux))
- Mois futurs : CA_2025 du mois × (1+taux)
"""
from __future__ import annotations

import calendar
import datetime
import logging
import re
from typing import Any

_log = logging.getLogger("ferment.commercial")

_MOIS_LABELS = [
    "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]

_MOIS_MAP = {
    "janv": 1, "févr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
    "juil": 7, "août": 8, "aout": 8, "sept": 9, "oct": 10, "nov": 11, "déc": 12,
}


# ─── Parsing ─────────────────────────────────────────────────────────────────

def _parse_monthly_series(data: dict[str, Any]) -> tuple[dict[int, float], dict[int, float]]:
    """Parse get_ca_mensuel() → (ca_current_year, ca_reference_year) par mois."""
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
            month = _month_from_label(x)
            if month is not None:
                target[month] = float(y)

    return ca_current, ca_ref


def _month_from_label(label: str) -> int | None:
    """'janv. 2026' → 1, 'mars 2025' → 3, etc."""
    if not label:
        return None
    word = label.split()[0].rstrip(".")
    for key, num in _MOIS_MAP.items():
        if word.startswith(key) or key.startswith(word):
            return num
    return None


def _sum_daily_series(data: dict[str, Any]) -> float:
    """Somme les valeurs journalières d'un ModeleIndicateurResultat."""
    series = data.get("series") or []
    if not series:
        return 0.0
    total = 0.0
    for v in (series[0].get("values") or []):
        y = v.get("y")
        if y is not None:
            total += float(y)
    return total


def _daily_by_day(data: dict[str, Any]) -> dict[int, float]:
    """Parse données journalières → {day_of_month: CA}.

    Le champ x est au format DD/MM/YYYY.
    """
    series = data.get("series") or []
    if not series:
        return {}
    result: dict[int, float] = {}
    for v in (series[0].get("values") or []):
        x = str(v.get("x") or "")
        y = v.get("y")
        if y is None:
            continue
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", x)
        if m:
            day = int(m.group(1))
            result[day] = result.get(day, 0.0) + float(y)
    return result


# ─── Calcul principal ────────────────────────────────────────────────────────

def fetch_ca_comparison(
    year_a: int = 2025,
    year_b: int = 2026,
) -> dict[str, Any]:
    """Fetch CA mensuel + calcul prévisions basées sur 2 mois glissants.

    Logique :
    1. Appel mensuel (12 mois N et N-1) pour le graphique
    2. Appel journalier sur fenêtre [J-60 → J] en N et N-1 pour le taux
    3. Appel journalier du mois en cours en N-1 pour le prorata fin de mois
    4. Prévision = CA N-1 × (1 + taux)
    """
    from common.easybeer.indicators import get_ca_daily, get_ca_mensuel

    now = datetime.datetime.now(datetime.UTC)
    today = now.date()
    current_month = today.month
    current_day = today.day

    # ── 1. CA mensuel (12 mois avec référence N-1) ──
    _log.info("Fetching CA mensuel %d...", year_b)
    data_mensuel = get_ca_mensuel(year_b, include_avoir=True, include_carnet=True)
    ca_b_monthly, ca_a_monthly = _parse_monthly_series(data_mensuel)

    # ── 2. Taux de croissance sur fenêtre glissante 2 mois ──
    window_start_b = today - datetime.timedelta(days=60)
    window_start_a = window_start_b.replace(year=year_a)
    window_end_a = today.replace(year=year_a)

    _log.info(
        "Fetching CA daily pour taux : %d(%s→%s) vs %d(%s→%s)",
        year_b, window_start_b, today,
        year_a, window_start_a, window_end_a,
    )

    data_window_b = get_ca_daily(
        f"{window_start_b.isoformat()}T00:00:00.000Z",
        f"{today.isoformat()}T23:59:59.999Z",
    )
    ca_window_b = _sum_daily_series(data_window_b)

    data_window_a = get_ca_daily(
        f"{window_start_a.isoformat()}T00:00:00.000Z",
        f"{window_end_a.isoformat()}T23:59:59.999Z",
    )
    ca_window_a = _sum_daily_series(data_window_a)

    if ca_window_a > 0:
        growth_rate = ca_window_b / ca_window_a - 1
    else:
        growth_rate = 0.0

    _log.info(
        "Taux glissant 2 mois : CA %d=%.0f, CA %d=%.0f → %+.1f%%",
        year_b, ca_window_b, year_a, ca_window_a, growth_rate * 100,
    )

    # ── 3. Prévision fin de mois en cours ──
    # CA 2025 du mois en cours : jours restants (current_day+1 → fin du mois)
    days_in_month = calendar.monthrange(year_a, current_month)[1]
    ca_a_current_full = ca_a_monthly.get(current_month, 0.0)

    # Prorata : CA_2025 des jours restants ≈ CA_2025_mensuel × (jours_restants / jours_total)
    days_remaining = days_in_month - current_day
    if days_in_month > 0 and ca_a_current_full > 0:
        ca_a_remaining = ca_a_current_full * (days_remaining / days_in_month)
        forecast_remaining = ca_a_remaining * (1 + growth_rate)
    else:
        forecast_remaining = 0.0

    # ── 4. Construire le tableau mensuel ──
    months: list[dict[str, Any]] = []
    ytd_a = 0.0
    ytd_b = 0.0
    ca_cible = 0.0

    for m in range(1, 13):
        a = ca_a_monthly.get(m, 0.0)
        b = ca_b_monthly.get(m, 0.0)

        if m < current_month:
            # Mois passé complet
            realized = b
            forecast = 0.0
            ca_cible += b
        elif m == current_month:
            # Mois en cours : réalisé + prévision du reste
            realized = b
            forecast = max(0.0, forecast_remaining)
            ca_cible += b + forecast
        else:
            # Mois futur : prévision complète
            realized = 0.0
            forecast = a * (1 + growth_rate) if a > 0 else 0.0
            ca_cible += forecast

        # % évolution
        if m < current_month:
            pct = round((b - a) / a * 100, 1) if a > 0 else (100.0 if b > 0 else 0.0)
        elif m == current_month:
            total_b = realized + forecast
            pct = round((total_b - a) / a * 100, 1) if a > 0 else 0.0
        else:
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

        # Cumul YTD à date
        if m < current_month:
            ytd_a += a
            ytd_b += b
        elif m == current_month:
            # Prorata jour pour année A
            ratio = current_day / days_in_month if days_in_month > 0 else 1.0
            ytd_a += a * ratio
            ytd_b += b

    ytd_pct = round((ytd_b - ytd_a) / ytd_a * 100, 1) if ytd_a > 0 else 0.0

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
        "ca_window_a": round(ca_window_a, 2),
        "ca_window_b": round(ca_window_b, 2),
    }
