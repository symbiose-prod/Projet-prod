"""
common/services/forecast_service.py
===================================
Service de prévision des ventes par goût pour les 6 prochains mois.

Modèle :
    prévision(goût, mois) = ventes_2025(goût, mois) × facteur_tendance(goût)
    facteur_tendance(goût) = somme(ventes_2026[2 derniers mois clos] / ventes_2025[mêmes 2 mois])

Ne dépend ni de NiceGUI ni de pages/. Lit le cache DB (``monthly_sales``).
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field

from common.sales_cache import get_monthly_sales

_log = logging.getLogger("ferment.forecast")


@dataclass
class ForecastResult:
    """Résultat de prévision pour un horizon de 6 mois."""

    months: list[tuple[int, int]] = field(default_factory=list)
    """Liste ordonnée des (year, month) prévus."""

    forecast: dict[tuple[int, int, str], float] = field(default_factory=dict)
    """{(year, month, gout_canon): volume_hl_prévu}."""

    trend_factor: dict[str, float] = field(default_factory=dict)
    """{gout_canon: facteur de tendance 2026/2025}."""

    baseline_year: int = 2025
    """Année de référence pour la saisonnalité."""

    last_closed_months: list[tuple[int, int]] = field(default_factory=list)
    """Mois 2026 utilisés pour le calcul du facteur de tendance."""


def _today() -> _dt.date:
    return _dt.date.today()


def _last_closed_2026_months(today: _dt.date | None = None, n: int = 2) -> list[tuple[int, int]]:
    """Retourne les ``n`` derniers mois 2026 entièrement clos (mois précédant le mois courant)."""
    today = today or _today()
    if today.year < 2026:
        return []
    last_month = today.month - 1 if today.month > 1 else 0
    if today.year > 2026 and last_month == 0:
        last_month = 12
    closed: list[tuple[int, int]] = []
    y, m = (2026, last_month) if today.year == 2026 else (2026, 12)
    while m >= 1 and len(closed) < n:
        closed.append((y, m))
        m -= 1
    return list(reversed(closed))


def _next_n_months(start_year: int, start_month: int, n: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = start_year, start_month
    for _ in range(n):
        out.append((y, m))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return out


def compute_forecast(
    tenant_id: str,
    horizon_months: int = 6,
    *,
    baseline_year: int = 2025,
    today: _dt.date | None = None,
) -> ForecastResult:
    """Calcule la prévision sur ``horizon_months`` mois à partir du mois courant.

    Lit le cache DB (``monthly_sales``). Si les données 2025 manquent → prévisions
    vides. Si les données 2026 manquent → facteur de tendance = 1 (saisonnalité pure).
    """
    today = today or _today()

    # Mois à prévoir : mois courant + horizon_months - 1
    target_months = _next_n_months(today.year, today.month, horizon_months)

    # Données de référence : baseline + 2026 pour facteur tendance
    closed_2026 = _last_closed_2026_months(today, n=2)
    years_to_load = sorted({baseline_year} | {y for y, _ in closed_2026})
    cache = get_monthly_sales(tenant_id, years_to_load)

    if not cache:
        _log.warning("compute_forecast: cache DB vide pour tenant=%s", tenant_id)
        return ForecastResult(
            months=target_months,
            baseline_year=baseline_year,
            last_closed_months=closed_2026,
        )

    # Index par goût pour le baseline 2025
    gouts: set[str] = {g for (y, _, g) in cache.keys() if y == baseline_year}

    # Facteur de tendance par goût
    trend_factor: dict[str, float] = {}
    for g in gouts:
        sum_2026 = sum(cache.get((y, m, g), 0.0) for (y, m) in closed_2026)
        sum_baseline = sum(cache.get((baseline_year, m, g), 0.0) for (_, m) in closed_2026)
        if sum_baseline > 0.001:
            trend_factor[g] = sum_2026 / sum_baseline
        else:
            trend_factor[g] = 1.0

    # Prévision goût × mois
    forecast: dict[tuple[int, int, str], float] = {}
    for (y, m) in target_months:
        for g in gouts:
            base = cache.get((baseline_year, m, g), 0.0)
            forecast[(y, m, g)] = round(base * trend_factor[g], 2)

    return ForecastResult(
        months=target_months,
        forecast=forecast,
        trend_factor=trend_factor,
        baseline_year=baseline_year,
        last_closed_months=closed_2026,
    )
