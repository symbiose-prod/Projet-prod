"""
common/services/production_slots.py
===================================
Assignation des prévisions de demande aux slots de production hebdomadaires.

Règles métier (Symbiose Kéfir) :
- Vendredi : brassin 5200L (utile ~4800L après pertes)
- Lundi    : brassin 7200L (utile ~6800L après pertes)
- Embouteillage des deux a lieu la semaine suivante.

L'algorithme alloue, semaine par semaine, le goût le plus en retard sur sa
cible cumulative aux deux slots disponibles.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass

from common.services.forecast_service import ForecastResult

_log = logging.getLogger("ferment.production_slots")

VOL_FRIDAY_HL = 48.0   # 5200L brut → ~4800L utile, on prend 4800/100 = 48 hL utiles
VOL_MONDAY_HL = 68.0   # 7200L brut → ~6800L utile


@dataclass
class ProductionSlot:
    date: _dt.date
    slot_type: str        # "Vendredi 5200L" | "Lundi 7200L"
    volume_hl: float      # volume utile attendu après pertes
    gout: str | None = None


@dataclass
class ProductionWeek:
    week_start: _dt.date  # lundi de la semaine
    iso_year: int
    iso_week: int
    monday_slot: ProductionSlot
    friday_slot: ProductionSlot


def _next_monday(d: _dt.date) -> _dt.date:
    days = (7 - d.weekday()) % 7
    return d + _dt.timedelta(days=days or 7) if d.weekday() != 0 else d


def _week_in_month(week_start: _dt.date) -> tuple[int, int]:
    """Mois principal de la semaine (jeudi de référence ISO)."""
    thursday = week_start + _dt.timedelta(days=3)
    return thursday.year, thursday.month


def assign_slots(
    forecast: ForecastResult,
    *,
    today: _dt.date | None = None,
    nb_weeks: int = 26,
) -> list[ProductionWeek]:
    """Assigne chaque slot de production au goût le plus en retard sur sa cible."""
    today = today or _dt.date.today()
    start_monday = today if today.weekday() == 0 else _next_monday(today)

    # Cumul demande prévue par mois → on convertit en cible cumulée par semaine
    monthly_demand: dict[tuple[int, int], dict[str, float]] = {}
    for (y, m, g), v in forecast.forecast.items():
        monthly_demand.setdefault((y, m), {})[g] = monthly_demand.get((y, m), {}).get(g, 0.0) + v

    weeks: list[ProductionWeek] = []
    produced: dict[str, float] = {}  # cumul produit par goût

    for w in range(nb_weeks):
        wk_start = start_monday + _dt.timedelta(weeks=w)
        y, m = _week_in_month(wk_start)
        iso = wk_start.isocalendar()

        # Cible cumulée jusqu'à fin de cette semaine = somme des demandes mensuelles
        # des mois passés + prorata du mois courant en fonction de la position de la semaine
        target: dict[str, float] = {}
        for (yy, mm), goutmap in monthly_demand.items():
            if (yy, mm) < (y, m):
                for g, v in goutmap.items():
                    target[g] = target.get(g, 0.0) + v
            elif (yy, mm) == (y, m):
                # Prorata : combien de semaines de ce mois sont déjà passées (incluant celle-ci)
                weeks_in_month = _weeks_in_month(yy, mm)
                week_idx = _week_index_in_month(wk_start)
                ratio = min(1.0, week_idx / max(weeks_in_month, 1))
                for g, v in goutmap.items():
                    target[g] = target.get(g, 0.0) + v * ratio

        # Reste à produire par goût = cible - déjà produit
        remaining = {g: target.get(g, 0.0) - produced.get(g, 0.0) for g in target.keys()}
        sorted_goutsranked = sorted(remaining.items(), key=lambda kv: kv[1], reverse=True)

        # Lundi 7200L → goût le plus en retard
        # Vendredi 5200L → 2ème en retard (ou même goût si le déficit est énorme)
        monday_gout = sorted_goutsranked[0][0] if sorted_goutsranked and sorted_goutsranked[0][1] > 0 else None
        friday_gout = None
        if len(sorted_goutsranked) >= 2 and sorted_goutsranked[1][1] > 0:
            friday_gout = sorted_goutsranked[1][0]
        elif monday_gout and sorted_goutsranked[0][1] > VOL_MONDAY_HL + VOL_FRIDAY_HL:
            friday_gout = monday_gout  # double brassin si forte demande

        if monday_gout:
            produced[monday_gout] = produced.get(monday_gout, 0.0) + VOL_MONDAY_HL
        if friday_gout:
            produced[friday_gout] = produced.get(friday_gout, 0.0) + VOL_FRIDAY_HL

        weeks.append(ProductionWeek(
            week_start=wk_start,
            iso_year=iso.year,
            iso_week=iso.week,
            monday_slot=ProductionSlot(
                date=wk_start, slot_type="Lundi 7200L",
                volume_hl=VOL_MONDAY_HL, gout=monday_gout,
            ),
            friday_slot=ProductionSlot(
                date=wk_start + _dt.timedelta(days=4), slot_type="Vendredi 5200L",
                volume_hl=VOL_FRIDAY_HL, gout=friday_gout,
            ),
        ))

    return weeks


def _weeks_in_month(year: int, month: int) -> int:
    """Nombre de semaines ISO chevauchant ce mois (4 ou 5)."""
    first = _dt.date(year, month, 1)
    if month == 12:
        last = _dt.date(year, 12, 31)
    else:
        last = _dt.date(year, month + 1, 1) - _dt.timedelta(days=1)
    return ((last - first).days // 7) + 1


def _week_index_in_month(d: _dt.date) -> int:
    """1-indexed : la 1ère semaine du mois est 1, etc."""
    return ((d.day - 1) // 7) + 1
