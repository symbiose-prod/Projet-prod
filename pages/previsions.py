"""
pages/previsions.py
===================
Page Prévisions — Planification visuelle des productions sur les 6 prochains mois.

Utilise :
- ``common.sales_cache``    pour la source de données (cache DB → API EB)
- ``common.services.forecast_service``   pour le calcul saisonnalité × tendance
- ``common.services.production_slots``   pour l'assignation aux slots
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import logging
from collections import defaultdict

from nicegui import ui

from common.data import get_paths
from common.easybeer import is_configured as eb_configured
from common.sales_cache import ensure_history_synced, get_sync_status
from common.services.forecast_service import compute_forecast
from common.services.production_slots import assign_slots
from core.optimizer import load_flavor_map_from_path
from pages.auth import require_auth
from pages.theme import COLORS, kpi_card, page_layout, section_title

_log = logging.getLogger("ferment.previsions")

_MONTH_NAMES_FR = [
    "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]


def _gout_color(gout: str) -> str:
    """Palette stable par goût — hash du nom → couleur HSL pastel."""
    if not gout:
        return "#E5E7EB"  # gris neutre
    h = int(hashlib.md5(gout.encode("utf-8")).hexdigest()[:8], 16)
    hue = h % 360
    return f"hsl({hue}, 62%, 68%)"


def _gout_text_color(gout: str) -> str:
    """Texte sombre sur fond pastel."""
    return "#1F2937"


def _fmt_dt(d: _dt.datetime | None) -> str:
    if d is None:
        return "—"
    if isinstance(d, str):
        return d
    return d.strftime("%d/%m/%Y %H:%M")


@ui.page("/previsions")
def page_previsions():
    user = require_auth()
    if not user:
        return

    tenant_id = user.get("tenant_id", "")

    with page_layout("Prévisions", "insights", "/previsions") as sidebar:
        with sidebar:
            ui.label("Horizon 6 mois").classes("text-subtitle2 text-grey-7")
            ui.label(
                "Planification hebdomadaire des productions, basée sur la "
                "saisonnalité 2025 × tendance 2026."
            ).classes("text-caption text-grey-5")

        if not eb_configured():
            with ui.card().classes("w-full q-pa-md").props("flat bordered"):
                ui.label("EasyBeer non configuré.").classes("text-body1")
            return

        # ── Conteneur principal qui sera rechargé après sync ──────────
        main = ui.column().classes("w-full gap-4")

        def render():
            main.clear()
            with main:
                _render_content(tenant_id, render)

        render()


def _render_content(tenant_id: str, refresh_fn):
    """Rendu du contenu principal (KPI + sync + calendrier)."""
    today = _dt.date.today()

    # ── Status de la sync historique ───────────────────────────────
    status = get_sync_status(tenant_id, 2025, today.year)
    months_needed = _months_to_sync(today)
    months_cached = sum(1 for ym in months_needed if ym in status)
    months_missing = len(months_needed) - months_cached

    last_sync = None
    for _, fetched in status.items():
        if fetched and (last_sync is None or fetched > last_sync):
            last_sync = fetched

    # ── KPI cards ──────────────────────────────────────────────────
    with ui.row().classes("w-full gap-3"):
        kpi_card(
            "history",
            "Historique cached",
            f"{months_cached}/{len(months_needed)} mois",
            COLORS["green"] if months_missing == 0 else COLORS["warning"],
        )
        kpi_card(
            "sync",
            "Dernière sync",
            _fmt_dt(last_sync),
            COLORS["success"] if last_sync else COLORS["ink2"],
        )

    # ── Synchronisation ────────────────────────────────────────────
    _render_sync_section(tenant_id, months_missing, refresh_fn)

    # ── Prévision + calendrier (si cache suffisant) ────────────────
    forecast = compute_forecast(tenant_id, horizon_months=6, today=today)
    if not forecast.forecast:
        with ui.card().classes("w-full q-pa-md").props("flat bordered"):
            ui.label(
                "Pas encore de données en cache. Lance la synchronisation "
                "pour générer les prévisions."
            ).classes("text-body1").style(f"color: {COLORS['ink2']}")
        return

    section_title("Vue d'ensemble", "summarize")
    _render_forecast_summary(forecast)

    section_title("Planning production (26 semaines)", "calendar_view_month")
    weeks = assign_slots(forecast, today=today, nb_weeks=26)
    _render_calendar(weeks)


def _render_sync_section(tenant_id: str, months_missing: int, refresh_fn):
    with ui.card().classes("w-full").props("flat bordered"):
        with ui.card_section().classes("q-pa-md"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("cloud_sync", size="sm").style(f"color: {COLORS['green']}")
                ui.label("Synchronisation historique EasyBeer").classes("text-h6")

            msg = (
                f"{months_missing} mois à synchroniser. Clique pour récupérer "
                "les ventes manquantes depuis EasyBeer."
                if months_missing
                else "Historique à jour. Clique pour rafraîchir le mois en cours."
            )
            ui.label(msg).classes("text-body2 q-mt-xs").style(f"color: {COLORS['ink2']}")

            progress_label = ui.label("").classes("text-body2 q-mt-sm")
            progress_bar = ui.linear_progress(value=0.0).classes("w-full q-mt-sm")
            progress_label.set_visibility(False)
            progress_bar.set_visibility(False)

            sync_btn = ui.button(
                "Synchroniser maintenant",
                icon="download",
            ).props("unelevated color=green-8")

            async def do_sync():
                sync_btn.disable()
                progress_label.set_visibility(True)
                progress_bar.set_visibility(True)
                try:
                    _, fm_path, _ = get_paths()
                    fm = load_flavor_map_from_path(fm_path)

                    def _progress(i, total, y, m):
                        progress_label.text = f"Sync {y}-{m:02d} ({i}/{total})…"
                        progress_bar.value = i / max(total, 1)

                    today = _dt.date.today()
                    result = await asyncio.to_thread(
                        ensure_history_synced,
                        tenant_id, fm,
                        year_from=2025, month_from=1,
                        year_to=today.year, month_to=today.month,
                        force_refresh_current=True,
                        progress_callback=_progress,
                    )
                    if result["errors"]:
                        ui.notify(
                            f"Sync terminée avec {len(result['errors'])} erreur(s)",
                            type="warning",
                        )
                    else:
                        ui.notify(
                            f"Sync OK — {result['synced']} mois mis à jour",
                            type="positive",
                        )
                except Exception as exc:
                    _log.warning("Erreur sync historique", exc_info=True)
                    ui.notify(f"Erreur sync : {exc}", type="negative")
                finally:
                    sync_btn.enable()
                    progress_label.set_visibility(False)
                    progress_bar.set_visibility(False)
                    refresh_fn()

            sync_btn.on_click(do_sync)


def _render_forecast_summary(forecast):
    from common.services.production_slots import VOL_FRIDAY_HL, VOL_MONDAY_HL

    total_hl = sum(forecast.forecast.values())
    by_gout: dict[str, float] = defaultdict(float)
    for (_, _, g), v in forecast.forecast.items():
        by_gout[g] += v

    # Capacité max sur 26 semaines = 26 × (Lundi 7200 + Vendredi 5200) en hL utiles
    max_capacity_hl = 26 * (VOL_MONDAY_HL + VOL_FRIDAY_HL)
    saturation = total_hl / max_capacity_hl * 100

    with ui.card().classes("w-full").props("flat bordered"):
        with ui.card_section().classes("q-pa-md"):
            ui.label(
                f"Volume prévu sur 6 mois : {total_hl:.0f} hL "
                f"({total_hl/6:.0f} hL/mois) — saturation {saturation:.0f}% de la capacité"
            ).classes("text-body1").style("font-weight: 500")

            if saturation > 100:
                with ui.element("div").style(
                    "background: #FEF3C7; color: #92400E; "
                    "padding: 8px 12px; border-radius: 6px; "
                    "font-size: 13px; margin-top: 8px;"
                ):
                    ui.html(
                        f"⚠️ <b>Demande > capacité</b> — "
                        f"il manque {(total_hl - max_capacity_hl):.0f} hL "
                        "de capacité sur l'horizon. Tous les slots seront remplis et "
                        "les goûts à plus faible volume seront sous-produits. "
                        "Envisage d'ajouter des journées de production ou de revoir le facteur de tendance."
                    )

            with ui.row().classes("w-full gap-2 q-mt-sm flex-wrap"):
                for g in sorted(by_gout, key=by_gout.get, reverse=True):
                    vol = by_gout[g]
                    trend = forecast.trend_factor.get(g, 1.0)
                    color = _gout_color(g)
                    trend_pct = (trend - 1) * 100
                    trend_sign = "+" if trend_pct >= 0 else ""
                    with ui.element("div").style(
                        f"background: {color}; color: {_gout_text_color(g)}; "
                        "padding: 8px 12px; border-radius: 8px; "
                        "font-size: 13px; font-weight: 500;"
                    ):
                        ui.html(
                            f"{g}<br>"
                            f"<b>{vol:.0f} hL</b> "
                            f"<span style='font-size:11px;opacity:0.75'>"
                            f"(tendance {trend_sign}{trend_pct:.0f}%)</span>"
                        )


def _render_calendar(weeks):
    # Regroupe les semaines par mois principal
    by_month: dict[tuple[int, int], list] = defaultdict(list)
    for wk in weeks:
        thursday = wk.week_start + _dt.timedelta(days=3)
        by_month[(thursday.year, thursday.month)].append(wk)

    for (y, m) in sorted(by_month):
        month_name = _MONTH_NAMES_FR[m]
        with ui.card().classes("w-full q-mb-md").props("flat bordered"):
            with ui.card_section().classes("q-pa-md"):
                ui.label(f"{month_name} {y}").classes("text-h6").style(
                    f"color: {COLORS['green']}; font-weight: 600"
                )
                with ui.row().classes("w-full gap-2 q-mt-sm flex-wrap"):
                    for wk in by_month[(y, m)]:
                        _render_week_card(wk)


def _render_week_card(wk):
    with ui.element("div").style(
        f"border: 1px solid {COLORS['border']}; border-radius: 8px; "
        "padding: 10px 12px; min-width: 210px; flex: 1 1 210px; "
        "background: #FFFFFF;"
    ):
        ui.label(
            f"Semaine {wk.iso_week} "
            f"({wk.week_start.strftime('%d/%m')})"
        ).classes("text-caption text-grey-6").style("font-weight: 500")

        _render_slot_pill(wk.monday_slot, "Lundi", "7 200 L")
        _render_slot_pill(wk.friday_slot, "Vendredi", "5 200 L")


def _render_slot_pill(slot, day_label: str, volume_label: str):
    gout = slot.gout or ""
    color = _gout_color(gout) if gout else "#F3F4F6"
    text_color = _gout_text_color(gout) if gout else COLORS["ink2"]
    with ui.element("div").style(
        f"background: {color}; color: {text_color}; "
        "margin-top: 6px; padding: 7px 10px; border-radius: 6px; "
        "font-size: 12px; line-height: 1.3;"
    ):
        ui.html(
            f"<div style='font-weight:600'>{day_label} — {volume_label}</div>"
            f"<div>{gout or '—'}</div>"
        )


def _months_to_sync(today: _dt.date) -> list[tuple[int, int]]:
    """De janv 2025 jusqu'au mois courant."""
    out: list[tuple[int, int]] = []
    y, m = 2025, 1
    while (y, m) <= (today.year, today.month):
        out.append((y, m))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return out
