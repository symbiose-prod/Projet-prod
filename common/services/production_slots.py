"""
common/services/production_slots.py
===================================
Assignation des prévisions de demande aux slots de production hebdomadaires.

Règles métier (Symbiose Kéfir) :
- Vendredi : brassin 5200L (utile ~4800L après pertes)
- Lundi    : brassin 7200L (utile ~6800L après pertes)
- Embouteillage des deux a lieu en semaine S+1.

L'algorithme est "lazy" : un slot n'est rempli que si la demande cumulée
jusqu'à fin S+1 dépasse ce qui a déjà été produit d'un montant supérieur
au seuil minimum (par défaut 40% du volume du slot). Sinon le slot reste
vide — évite le surstock.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass

from common.services.forecast_service import ForecastResult

_log = logging.getLogger("ferment.production_slots")

VOL_FRIDAY_HL = 48.0   # 5200L brut → ~4800L utile
VOL_MONDAY_HL = 68.0   # 7200L brut → ~6800L utile

# Lead time (semaines) : la production en semaine S est embouteillée en S+1.
# On inclut donc la demande jusqu'à fin S+LEAD_TIME dans la cible cumulée.
LEAD_TIME_WEEKS = 1

# Seuil minimum : un slot n'est rempli que si le déficit du top goût atteint
# au moins cette fraction du volume du slot. Évite les slots "presque vides".
DEFAULT_MIN_DEFICIT_RATIO = 0.4


@dataclass
class ProductionSlot:
    date: _dt.date
    slot_type: str        # "Vendredi 5200L" | "Lundi 7200L"
    volume_hl: float      # volume utile attendu
    gout: str | None = None


@dataclass
class ProductionWeek:
    week_start: _dt.date  # lundi de la semaine
    iso_year: int
    iso_week: int
    monday_slot: ProductionSlot
    friday_slot: ProductionSlot


def _next_monday(d: _dt.date) -> _dt.date:
    """Retourne le lundi suivant (ou lui-même si d est déjà un lundi)."""
    if d.weekday() == 0:
        return d
    days = (7 - d.weekday()) % 7
    return d + _dt.timedelta(days=days)


def _week_in_month(week_start: _dt.date) -> tuple[int, int]:
    """Mois principal de la semaine (jeudi de référence ISO)."""
    thursday = week_start + _dt.timedelta(days=3)
    return thursday.year, thursday.month


def _weeks_in_month(year: int, month: int) -> int:
    """Nombre approximatif de semaines dans le mois (pour prorata)."""
    first = _dt.date(year, month, 1)
    last = (
        _dt.date(year, 12, 31)
        if month == 12
        else _dt.date(year, month + 1, 1) - _dt.timedelta(days=1)
    )
    return ((last - first).days // 7) + 1


def _cumulative_target_up_to(
    monthly_demand: dict[tuple[int, int], dict[str, float]],
    target_end: _dt.date,
) -> dict[str, float]:
    """Demande cumulée par goût depuis le début de l'horizon jusqu'à target_end (inclus).

    Pour le mois contenant target_end, on prend un prorata linéaire sur le nb
    de jours du mois couverts par target_end.
    """
    target: dict[str, float] = {}
    for (y, m), goutmap in monthly_demand.items():
        last_day_of_month = (
            _dt.date(y, 12, 31)
            if m == 12
            else _dt.date(y, m + 1, 1) - _dt.timedelta(days=1)
        )
        if target_end >= last_day_of_month:
            ratio = 1.0
        elif target_end < _dt.date(y, m, 1):
            ratio = 0.0
        else:
            days_in_month = last_day_of_month.day
            ratio = target_end.day / days_in_month
        for g, v in goutmap.items():
            target[g] = target.get(g, 0.0) + v * ratio
    return target


def _pick_top_gout(
    target: dict[str, float], produced: dict[str, float],
    slot_volume: float, min_ratio: float,
) -> str | None:
    """Retourne le goût avec le plus gros déficit, ou None si sous le seuil."""
    if not target:
        return None
    deficits = {g: target.get(g, 0.0) - produced.get(g, 0.0) for g in target}
    top_g, top_def = max(deficits.items(), key=lambda kv: kv[1])
    if top_def >= min_ratio * slot_volume:
        return top_g
    return None


def assign_slots(
    forecast: ForecastResult,
    *,
    today: _dt.date | None = None,
    nb_weeks: int = 26,
    min_deficit_ratio: float = DEFAULT_MIN_DEFICIT_RATIO,
) -> list[ProductionWeek]:
    """Assigne les slots Lundi/Vendredi au goût le plus en retard.

    Un slot reste vide si le déficit du top goût n'atteint pas
    ``min_deficit_ratio × volume_slot`` — évite le surstock.
    """
    today = today or _dt.date.today()
    start_monday = _next_monday(today)

    # Regroupe la prévision par (année, mois) → {goût: volume_hl}
    monthly_demand: dict[tuple[int, int], dict[str, float]] = {}
    for (y, m, g), v in forecast.forecast.items():
        monthly_demand.setdefault((y, m), {})[g] = (
            monthly_demand.get((y, m), {}).get(g, 0.0) + v
        )

    weeks: list[ProductionWeek] = []
    produced: dict[str, float] = {}

    for w in range(nb_weeks):
        wk_start = start_monday + _dt.timedelta(weeks=w)
        iso = wk_start.isocalendar()

        # Cible = demande cumulée jusqu'à la fin de la semaine S+LEAD_TIME
        target_end = (
            wk_start + _dt.timedelta(days=6 + 7 * LEAD_TIME_WEEKS)
        )
        target = _cumulative_target_up_to(monthly_demand, target_end)

        # Slot lundi (7200L) — sélection + incrémente produced
        monday_gout = _pick_top_gout(target, produced, VOL_MONDAY_HL, min_deficit_ratio)
        if monday_gout:
            produced[monday_gout] = produced.get(monday_gout, 0.0) + VOL_MONDAY_HL

        # Slot vendredi (5200L) — 2ème passe, évite de doubler le même goût
        friday_gout = _pick_top_gout(target, produced, VOL_FRIDAY_HL, min_deficit_ratio)
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
