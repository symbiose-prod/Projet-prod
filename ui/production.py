"""
ui/production.py
================
Page Production — Planning et création brassins.

Réutilise toute la logique métier de core/optimizer.py, common/easybeer.py,
common/xlsx_fill.py. Seule la couche UI (NiceGUI) est spécifique ici.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging

import pandas as pd
from nicegui import app, ui

from common.data import get_business_config, get_paths
from common.session_store import load_df, store_df
from common.xlsx_fill import fill_fiche_xlsx
from core.optimizer import (
    apply_canonical_flavor,
    load_flavor_map_from_path,
    sanitize_gouts,
)
from ui._production_calc import (
    _compute_production_sync,
    _fetch_eb_products,
)
from ui._production_easybeer import _render_easybeer_section
from ui.accueil import get_df_raw
from ui.auth import require_auth
from ui.theme import COLORS, date_picker_field, error_banner, kpi_card, page_layout, section_title

_log = logging.getLogger("ferment.production")

# ====== Constantes metier (chargées depuis config.yaml) ======
_biz = get_business_config()
DEFAULT_LOSS_LARGE = _biz["default_loss_large"]
DEFAULT_LOSS_SMALL = _biz["default_loss_small"]
DDM_DAYS = _biz["ddm_days"]

# ====== Configurations cuves ======
TANK_CONFIGS = _biz["tanks"]

TEMPLATE_PATH = "assets/Fiche_production.xlsx"


# ─── Page ───────────────────────────────────────────────────────────────────

@ui.page("/production")
async def page_production():
    user = require_auth()
    if not user:
        return

    # Avertissement navigateur si modifications non sauvegardées
    ui.add_head_html("""<script>
    window._fsProductionDirty = false;
    window.addEventListener('beforeunload', function(e) {
        if (window._fsProductionDirty) { e.preventDefault(); e.returnValue = ''; }
    });
    </script>""")

    with page_layout("Production", "factory", "/production") as sidebar:

        # ── Pré-requis : données importées ────────────────────────────
        df_raw, window_days = get_df_raw()

        if df_raw is None:
            with ui.card().classes("w-full q-pa-lg").props("flat bordered"):
                with ui.column().classes("items-center gap-3"):
                    ui.icon("info", size="xl").classes("text-grey-5")
                    ui.label("Aucune donnée importée").classes("text-h6 text-grey-6")
                    ui.label(
                        "Importe un fichier Excel ou synchronise avec EasyBeer "
                        "depuis la page Accueil."
                    ).classes("text-body2 text-grey-5")
                    ui.button(
                        "Aller à l'Accueil",
                        icon="home",
                        on_click=lambda: ui.navigate.to("/accueil"),
                    ).props("color=green-8 outline")
            return

        # ── Préparation des données ───────────────────────────────────
        _, flavor_map_path, images_dir = get_paths()
        fm = load_flavor_map_from_path(flavor_map_path)
        try:
            df_in = apply_canonical_flavor(df_raw, fm)
        except KeyError as e:
            ui.notify(str(e), type="negative")
            return
        df_in["Produit"] = df_in["Produit"].astype(str)
        df_in = sanitize_gouts(df_in)

        all_gouts = sorted(
            pd.Series(df_in.get("GoutCanon", pd.Series(dtype=str)))
            .dropna().astype(str).str.strip().unique()
        )

        # ── Sidebar : paramètres ──────────────────────────────────────
        with sidebar:
            ui.label("Paramètres").classes("text-subtitle2 text-grey-7")

            mode = ui.radio(
                list(TANK_CONFIGS.keys()),
                value="Cuve de 7200L (1 goût)",
            ).props("dense")

            # Input nb goûts (visible seulement en Split 7200L)
            split_container = ui.column().classes("w-full")

            ui.separator().classes("q-my-sm")
            ui.label("Filtres").classes("text-subtitle2 text-grey-7")

            repartir_cb = ui.checkbox("Au prorata des ventes", value=True)

            excluded_gouts_sel = ui.select(
                all_gouts,
                multiple=True,
                value=[],
                label="Exclure goûts",
            ).props("outlined dense use-chips").classes("w-full")

            # Exclusion par produit (Produit + Stock)
            try:
                df_preview = df_in.copy()
                df_preview["Produit complet"] = df_preview.apply(
                    lambda r: f"{r.get('Produit', '').strip()} — {r.get('Stock', '').strip()}"
                    if pd.notna(r.get("Stock")) else r.get("Produit", "").strip(),
                    axis=1,
                )
                product_options = sorted(df_preview["Produit complet"].dropna().unique().tolist())
            except Exception:
                _log.debug("Erreur chargement produits EasyBeer", exc_info=True)
                product_options = []

            excluded_products_sel = ui.select(
                product_options,
                multiple=True,
                value=[],
                label="Exclure produits",
            ).props("outlined dense use-chips").classes("w-full")

            forced_gouts_sel = ui.select(
                all_gouts,
                multiple=True,
                value=[],
                label="Forcer goûts",
            ).props("outlined dense use-chips").classes("w-full")

        # ── Conteneur principal (reconstruit à chaque recalcul) ───────
        main_container = ui.column().classes("w-full gap-5")

        # State persistant pour les overrides
        overrides: dict = app.storage.user.setdefault("production_overrides", {})

        # State pour les inputs Split 7200L
        nb_gouts_input_ref = {"ref": None}
        split_ratio_ref = {"ref": None}
        _split_label_ref = {"ref": None}

        _SPLIT_TOTAL = (
            TANK_CONFIGS["Split 7200L"]["capacity"]
            - TANK_CONFIGS["Split 7200L"]["transfer_loss"]
        )  # 6800 L
        _SPLIT_GARDE_CAP = TANK_CONFIGS["Split 7200L"]["split"]["garde_capacity"]  # 5200

        def _update_split_label(_=None):
            lbl = _split_label_ref["ref"]
            slider = split_ratio_ref["ref"]
            if lbl and slider:
                v1 = int(slider.value)
                v2 = _SPLIT_TOTAL - v1
                lbl.set_text(f"Goût 1 : {v1} L  |  Goût 2 : {v2} L")

        def _build_split_inputs():
            split_container.clear()
            split_ratio_ref["ref"] = None
            _split_label_ref["ref"] = None
            if mode.value == "Split 7200L":
                with split_container:
                    nb_gouts_input_ref["ref"] = ui.select(
                        {1: "1 goût", 2: "2 goûts"},
                        value=TANK_CONFIGS["Split 7200L"]["nb_gouts"],
                        label="Nb goûts",
                        on_change=lambda _: (_build_split_slider(), _debounced_compute()),
                    ).props("outlined dense").classes("w-full")
                    _build_split_slider()
            else:
                nb_gouts_input_ref["ref"] = None

        def _build_split_slider():
            """Affiche / masque le slider de répartition selon nb_gouts."""
            # Supprimer l'ancien slider s'il existe
            old = split_ratio_ref["ref"]
            if old and old.parent_slot and old.parent_slot.parent:
                try:
                    old.parent_slot.parent.remove(old)
                except Exception:
                    pass
            old_lbl = _split_label_ref["ref"]
            if old_lbl and old_lbl.parent_slot and old_lbl.parent_slot.parent:
                try:
                    old_lbl.parent_slot.parent.remove(old_lbl)
                except Exception:
                    pass
            split_ratio_ref["ref"] = None
            _split_label_ref["ref"] = None

            nb_ref = nb_gouts_input_ref["ref"]
            nb_val = int(nb_ref.value) if nb_ref else 1
            if mode.value == "Split 7200L" and nb_val >= 2:
                with split_container:
                    _half = _SPLIT_TOTAL // 2  # 3400
                    _split_label_ref["ref"] = ui.label(
                        f"Goût 1 : {_half} L  |  Goût 2 : {_half} L"
                    ).classes("text-caption text-grey-7 q-mt-xs")
                    split_ratio_ref["ref"] = (
                        ui.slider(
                            min=1000, max=min(_SPLIT_TOTAL - 1000, _SPLIT_GARDE_CAP),
                            step=100, value=_half,
                            on_change=lambda _: (_update_split_label(), _debounced_compute()),
                        ).props("label-always color=green-8").classes("w-full")
                    )

        async def do_compute():
            """Calcul complet : optimiseur + passe 2 + affichage (async)."""
            main_container.clear()
            with main_container:
                ui.spinner("dots", size="xl", color="green-8").classes("self-center q-pa-lg")

            # Paramètres (lecture UI — rapide)
            mode_prod = mode.value
            excluded_gouts = excluded_gouts_sel.value or []
            excluded_products = excluded_products_sel.value or []
            forced_gouts = forced_gouts_sel.value or []
            repartir_pro_rv = repartir_cb.value

            _tank = TANK_CONFIGS[mode_prod]
            volume_cible = _tank["nominal_hL"]
            if mode_prod == "Split 7200L":
                nb_ref = nb_gouts_input_ref["ref"]
                try:
                    nb_gouts = int(nb_ref.value) if nb_ref else _tank["nb_gouts"]
                except (TypeError, ValueError):
                    nb_gouts = _tank["nb_gouts"]
            else:
                nb_gouts = _tank["nb_gouts"]

            effective_nb_gouts = max(nb_gouts, len(forced_gouts)) if forced_gouts else nb_gouts

            # Répartition personnalisée (Split 7200L, 2 goûts)
            split_volumes = None
            if mode_prod == "Split 7200L" and effective_nb_gouts >= 2:
                _slider = split_ratio_ref["ref"]
                _v1 = int(_slider.value) if _slider else _SPLIT_TOTAL // 2
                split_volumes = [float(_v1), float(_SPLIT_TOTAL - _v1)]

            # Filtrage produits exclus
            if excluded_products:
                mask_excl = df_in.apply(
                    lambda r: f"{r.get('Produit', '').strip()} — {r.get('Stock', '').strip()}"
                    in excluded_products,
                    axis=1,
                )
                df_in_filtered = df_in.loc[~mask_excl].copy()
            else:
                df_in_filtered = df_in.copy()

            # ── Calcul lourd dans le thread pool ──────────────────────
            try:
                _result = await asyncio.wait_for(
                    asyncio.to_thread(
                        _compute_production_sync,
                        df_in_filtered, window_days, volume_cible,
                        effective_nb_gouts, repartir_pro_rv,
                        forced_gouts, excluded_gouts, mode_prod, overrides,
                        TANK_CONFIGS=TANK_CONFIGS,
                        DEFAULT_LOSS_LARGE=DEFAULT_LOSS_LARGE,
                        DEFAULT_LOSS_SMALL=DEFAULT_LOSS_SMALL,
                        split_volumes=split_volumes,
                    ),
                    timeout=60,
                )
            except TimeoutError:
                main_container.clear()
                with main_container:
                    error_banner(
                        "Le calcul a dépassé le délai (60 s). Réessayez avec moins de goûts ou un volume plus petit.",
                        retry_fn=do_compute,
                    )
                return
            except Exception as exc:
                main_container.clear()
                with main_container:
                    error_banner(
                        f"Erreur optimiseur : {exc}",
                        retry_fn=do_compute,
                    )
                return

            df_min = _result["df_min"]
            gouts_cibles = _result["gouts_cibles"]
            df_calc = _result["df_calc"]
            df_all = _result["df_all"]
            note_msg = _result["note_msg"]
            volume_details = _result["volume_details"]
            volume_cible = _result["volume_cible"]
            df_final = _result["df_final"]
            mp_check = _result.get("mp_check", {})
            ongoing = _result.get("ongoing", {})

            # ── Affichage ─────────────────────────────────────────────
            main_container.clear()
            with main_container:

                # Note d'ajustement
                if isinstance(note_msg, str) and note_msg.strip():
                    with ui.card().classes("w-full").props("flat bordered"):
                        with ui.card_section().classes("row items-center gap-2"):
                            ui.icon("info", size="sm").style(f"color: {COLORS['orange']}")
                            ui.label(note_msg).classes("text-body2")

                # Détails volume (modes auto)
                if volume_details:
                    for _g_vd, _vd in volume_details.items():
                        with ui.expansion(
                            f"Détails du calcul de volume — {_g_vd}",
                            icon="straighten",
                        ).classes("w-full"):
                            with ui.row().classes("w-full gap-4"):
                                kpi_card("science", "V départ (L)", f"{_vd['V_start']:.0f}", COLORS["green"])
                                kpi_card("opacity", "Aromatisation (L)", f"{_vd['V_aroma']:.0f}", COLORS["orange"])
                                kpi_card("local_drink", "V embouteillé (L)", f"{_vd['V_bottled']:.0f}", COLORS["blue"])
                                kpi_card("straighten", "Volume cible (hL)", f"{_vd['V_bottled']/100:.2f}", COLORS["green"])
                            ui.label(
                                f"Cuve {_vd['capacity']}L — "
                                f"Perte transfert : {_vd['transfer_loss']}L — "
                                f"Perte embouteillage : {_vd['bottling_loss']}L — "
                                f"Recette : {_vd['R']:.0f}L (réf) avec {_vd['A_R']:.1f}L d'aromatisation"
                            ).classes("text-caption text-grey-6 q-mt-sm")

                # ── Productions en cours ────────────────────────────
                _ong_detail = ongoing.get("detail", [])
                _ong_par_gout = ongoing.get("par_gout", {})
                _ong_total = ongoing.get("total_hl", 0.0)

                if _ong_detail:
                    _ong_nb = len(_ong_detail)
                    _ong_title = (
                        f"{_ong_nb} production{'s' if _ong_nb > 1 else ''}"
                        f" en cours — {_ong_total:.1f} hL"
                    )
                    with ui.expansion(
                        _ong_title,
                        icon="hourglass_top",
                        value=True,
                    ).classes("w-full").style(
                        f"border: 1px solid {COLORS['blue']}40; border-radius: 8px"
                    ):
                        _ong_rows = [
                            {
                                "brassin": d["nom"],
                                "produit": d["produit"],
                                "volume": f"{d['volume_l']:,} L".replace(",", " "),
                                "etat": d["etat"],
                                "date_cond": d["date_conditionnement"] or "—",
                                "_gout": d["gout"],
                                "_key": d["nom"],
                            }
                            for d in _ong_detail
                        ]
                        _ong_columns = [
                            {"name": "brassin", "label": "Brassin", "field": "brassin", "align": "left"},
                            {"name": "produit", "label": "Produit", "field": "produit", "align": "left"},
                            {"name": "volume", "label": "Volume", "field": "volume", "align": "right"},
                            {"name": "etat", "label": "État", "field": "etat", "align": "center"},
                            {"name": "date_cond", "label": "Conditionnement prévu", "field": "date_cond", "align": "center"},
                        ]
                        ui.table(
                            columns=_ong_columns,
                            rows=_ong_rows,
                            row_key="_key",
                        ).classes("w-full").props("flat bordered dense")

                        # Note explicative
                        _gouts_ajustes = [
                            f"{g} (+{v:.1f} hL)"
                            for g, v in _ong_par_gout.items()
                        ]
                        if _gouts_ajustes:
                            ui.label(
                                "ℹ️ Ces volumes ont été ajoutés au stock disponible "
                                "pour le calcul : " + ", ".join(_gouts_ajustes)
                            ).classes("text-caption text-grey-6 q-mt-sm")

                # KPIs
                total_btl = int(df_final["Bouteilles à produire (arrondi)"].sum()) if not df_final.empty else 0
                total_vol = float(df_final["Volume produit arrondi (hL)"].sum()) if not df_final.empty else 0.0
                nb_actifs = int((df_final["Cartons à produire (arrondi)"] > 0).sum()) if not df_final.empty else 0
                nb_forcés = int(df_final["_forcé"].sum()) if not df_final.empty else 0

                with ui.row().classes("w-full gap-4"):
                    kpi_card(
                        "local_drink", "Bouteilles à produire",
                        f"{total_btl:,}".replace(",", " "), COLORS["green"],
                    )
                    kpi_card(
                        "water_drop", "Volume total (hL)",
                        f"{total_vol:.2f}", COLORS["blue"],
                    )
                    kpi_card(
                        "category", "Formats en production",
                        f"{nb_actifs}" + (f" ({nb_forcés} forcé{'s' if nb_forcés > 1 else ''})" if nb_forcés else ""),
                        COLORS["orange"],
                    )

                # ── Vérification matières premières ───────────────────
                _mp_status = mp_check.get("status", "error")
                _mp_items = mp_check.get("items", [])
                _mp_err = mp_check.get("error_msg", "")
                _mp_shortages = [it for it in _mp_items if not it["ok"]]

                if _mp_status == "error":
                    if _mp_err:
                        with ui.row().classes("w-full items-center gap-2 q-py-xs"):
                            ui.icon("cloud_off", size="xs").classes("text-grey-5")
                            ui.label(f"Vérification MP indisponible — {_mp_err}").classes(
                                "text-caption text-grey-5"
                            )
                elif _mp_items:
                    _mp_color = COLORS["success"] if _mp_status == "ok" else COLORS["orange"]
                    _mp_icon = "check_circle" if _mp_status == "ok" else "warning"
                    _mp_title = (
                        "Matières premières disponibles"
                        if _mp_status == "ok"
                        else f"{len(_mp_shortages)} matière(s) première(s) insuffisante(s)"
                    )

                    with ui.expansion(
                        _mp_title,
                        icon=_mp_icon,
                        value=(_mp_status == "warning"),
                    ).classes("w-full").style(
                        f"border: 1px solid {_mp_color}40; border-radius: 8px"
                    ):
                        _mp_rows = [
                            {
                                "mp": it["libelle"],
                                "besoin": f"{it['besoin']:.1f} {it['unite']}",
                                "stock": f"{it['stock']:.1f} {it['unite']}",
                                "ecart": f"{it['ecart']:+.1f} {it['unite']}",
                                "statut": "OK" if it["ok"] else "Insuffisant",
                                "_ok": it["ok"],
                                "_key": str(it["id_mp"]),
                            }
                            for it in _mp_items
                        ]
                        _mp_columns = [
                            {"name": "mp", "label": "Matière première", "field": "mp", "align": "left"},
                            {"name": "besoin", "label": "Besoin", "field": "besoin", "align": "right"},
                            {"name": "stock", "label": "Stock", "field": "stock", "align": "right"},
                            {"name": "ecart", "label": "Écart", "field": "ecart", "align": "right"},
                            {"name": "statut", "label": "Statut", "field": "statut", "align": "center"},
                        ]
                        mp_table = ui.table(
                            columns=_mp_columns,
                            rows=_mp_rows,
                            row_key="_key",
                        ).classes("w-full").props("flat bordered dense")

                        mp_table.add_slot("body-cell-statut", r'''
                            <q-td :props="props">
                                <q-badge
                                    :color="props.row._ok ? 'green-7' : 'orange-8'"
                                    :label="props.row.statut"
                                    text-color="white"
                                />
                            </q-td>
                        ''')
                        mp_table.add_slot("body-cell-ecart", r'''
                            <q-td :props="props">
                                <span :style="{
                                    color: props.row._ok ? '#16A34A' : '#F97316',
                                    fontWeight: props.row._ok ? 400 : 700,
                                }">
                                    {{ props.row.ecart }}
                                </span>
                            </q-td>
                        ''')

                # ── Images produits EasyBeer ─────────────────────────
                product_images: dict[str, str] = {}  # produit_name → image_url
                try:
                    from common.easybeer import is_configured as _eb_img_conf
                    if _eb_img_conf():
                        _eb_prods_img = _fetch_eb_products()
                        for p in _eb_prods_img:
                            lbl = p.get("libelle", "")
                            urls = p.get("imagesUrl") or []
                            uri = p.get("imageUri") or ""
                            img = urls[0] if urls else uri
                            if img and lbl:
                                product_images[lbl] = img
                except Exception:
                    _log.debug("Erreur chargement image produit", exc_info=True)

                # ── Tableau de production ──────────────────────────────
                section_title("Plan de production", "assignment")

                if nb_forcés:
                    ui.label(
                        f"{nb_forcés} ligne(s) forcée(s) — le volume restant est redistribué."
                    ).classes("text-caption text-grey-6")

                if not df_final.empty:
                    # Construire les lignes triées : Symbiose d'abord, puis Niko
                    all_table_rows = []
                    for _, r in df_final.iterrows():
                        key = f"{r['GoutCanon']}|{r['Produit']}|{r['Stock']}"
                        produit_name = str(r["Produit"])
                        is_niko = "NIKO" in produit_name.upper()
                        all_table_rows.append({
                            "gout": str(r["GoutCanon"]),
                            "produit": produit_name,
                            "stock": str(r["Stock"]),
                            "forcer": overrides.get(key, None),
                            "cartons": int(r["Cartons à produire (arrondi)"]),
                            "bouteilles": int(r["Bouteilles à produire (arrondi)"]),
                            "volume": f"{float(r['Volume produit arrondi (hL)']):.3f}",
                            "_key": key,
                            "_brand": "Niko" if is_niko else "Symbiose Kéfir",
                        })

                    # Trier : Symbiose en premier, Niko ensuite
                    all_table_rows.sort(key=lambda r: (0 if r["_brand"] == "Symbiose Kéfir" else 1, r["gout"]))

                    # Images par marque
                    brand_images: dict[str, list[dict]] = {}
                    seen: set[str] = set()
                    for row in all_table_rows:
                        prod = row["produit"]
                        brand = row["_brand"]
                        if prod not in seen:
                            seen.add(prod)
                            img_url = product_images.get(prod, "")
                            if img_url:
                                brand_images.setdefault(brand, []).append({
                                    "gout": row["gout"], "url": img_url,
                                })

                    # Insérer des lignes séparatrices dans les données
                    ordered_rows: list[dict] = []
                    current_brand = None
                    for row in all_table_rows:
                        if row["_brand"] != current_brand:
                            current_brand = row["_brand"]
                            ordered_rows.append({
                                "_sep": True,
                                "_brand": current_brand,
                                "_brand_images": brand_images.get(current_brand, []),
                                "_key": f"_sep_{current_brand}",
                                "gout": "", "stock": "", "forcer": None,
                                "cartons": 0, "bouteilles": 0, "volume": "",
                                "produit": "",
                            })
                        ordered_rows.append(row)

                    columns = [
                        {"name": "gout", "label": "Goût", "field": "gout", "align": "left", "sortable": True},
                        {"name": "stock", "label": "Format", "field": "stock", "align": "left", "sortable": True},
                        {"name": "forcer", "label": "Forcer", "field": "forcer", "align": "right"},
                        {"name": "cartons", "label": "Cartons", "field": "cartons", "align": "right", "sortable": True},
                        {"name": "bouteilles", "label": "Bouteilles", "field": "bouteilles", "align": "right", "sortable": True},
                        {"name": "volume", "label": "Volume (hL)", "field": "volume", "align": "right", "sortable": True},
                    ]

                    nb_cols = len(columns)

                    table = ui.table(
                        columns=columns,
                        rows=ordered_rows,
                        row_key="_key",
                    ).classes("w-full").props("flat bordered dense")

                    # Slot body : séparateur marque OU ligne de données
                    table.add_slot("body", r'''
                        <q-tr v-if="props.row._sep" :props="props">
                            <q-td colspan="''' + str(nb_cols) + r'''"
                                   style="background: #F3F4F6; padding: 10px 12px; font-weight: 600; font-size: 13px; border-bottom: 2px solid #E5E7EB;">
                                <div style="display: flex; align-items: center; gap: 16px;">
                                    <span>{{ props.row._brand }}</span>
                                    <div v-if="props.row._brand_images && props.row._brand_images.length"
                                         style="display: flex; align-items: flex-end; gap: 10px; margin-left: 8px;">
                                        <div v-for="img in props.row._brand_images" :key="img.gout"
                                             style="display: flex; flex-direction: column; align-items: center; gap: 2px;">
                                            <img :src="img.url"
                                                 style="height: 48px; object-fit: contain; border-radius: 4px;" />
                                            <span style="font-size: 11px; color: #6B7280; font-weight: 400;">
                                                {{ img.gout }}
                                            </span>
                                        </div>
                                    </div>
                                </div>
                            </q-td>
                        </q-tr>
                        <q-tr v-else :props="props">
                            <q-td v-for="col in props.cols" :key="col.name" :props="props"
                                  :style="'text-align: ' + col.align">
                                <template v-if="col.name === 'forcer'">
                                    <span :style="{
                                        color: props.row.forcer != null && props.row.forcer !== '' ? '#F97316' : '#9CA3AF',
                                        fontWeight: props.row.forcer != null && props.row.forcer !== '' ? 700 : 400,
                                        cursor: 'pointer',
                                    }">
                                        {{ props.row.forcer != null && props.row.forcer !== '' ? props.row.forcer : 'auto' }}
                                        <q-icon name="edit" size="12px" color="grey-5" class="q-ml-xs" />
                                    </span>
                                    <q-popup-edit v-model="props.row.forcer" v-slot="scope"
                                        @update:model-value="() => $parent.$emit('forcer_update', props.row)">
                                        <q-input v-model.number="scope.value" type="number" dense autofocus
                                            placeholder="auto" min="0"
                                            input-class="text-right text-bold"
                                            style="min-width: 100px"
                                            hint="Entrée pour valider"
                                            @keyup.enter="scope.set" />
                                    </q-popup-edit>
                                </template>
                                <template v-else-if="col.name === 'cartons'">
                                    <span style="font-weight: 600;">{{ props.row[col.field] }}</span>
                                </template>
                                <template v-else>
                                    {{ props.row[col.field] }}
                                </template>
                            </q-td>
                        </q-tr>
                    ''')

                    # Clic sur cellule "Forcer" → popup → Entrée → applique override et recalcule
                    async def _on_forcer_update(e):
                        row = e.args
                        if not isinstance(row, dict) or row.get("_sep"):
                            return
                        key = row.get("_key", "")
                        val = row.get("forcer")
                        # val peut être : int, float, None, "" ou NaN (sérialisé en null)
                        if isinstance(val, (int, float)) and val == val and val > 0:
                            overrides[key] = int(val)
                        else:
                            overrides.pop(key, None)
                        app.storage.user["production_overrides"] = dict(overrides)
                        ui.run_javascript("window._fsProductionDirty = true;")
                        await do_compute()

                    table.on("forcer_update", _on_forcer_update)

                    with ui.row().classes("w-full gap-3 q-mt-sm"):

                        async def do_reset_overrides():
                            overrides.clear()
                            app.storage.user["production_overrides"] = {}
                            await do_compute()

                        ui.button(
                            "Réinitialiser",
                            icon="restart_alt",
                            on_click=do_reset_overrides,
                        ).props("flat color=grey-7")

                else:
                    ui.label(
                        "Aucun format disponible pour les goûts sélectionnés."
                    ).classes("text-grey-6 text-body1 q-pa-md")

                # ── df_min pour sauvegarde (>0 cartons uniquement) ────
                df_min_override = (
                    df_final[df_final["Cartons à produire (arrondi)"] > 0][[
                        "GoutCanon", "Produit", "Stock",
                        "Cartons à produire (arrondi)",
                        "Bouteilles à produire (arrondi)",
                        "Volume produit arrondi (hL)",
                    ]].copy().reset_index(drop=True)
                    if not df_final.empty else df_min.copy()
                )

                # ══════════════════════════════════════════════════════
                # ══════ Fiche de production + EasyBeer (côte à côte) ══
                # ══════════════════════════════════════════════════════
                with ui.row().classes("w-full gap-4 items-start"):

                    # ── Colonne gauche : Fiche de production ────────────
                    with ui.card().classes("flex-1").props("flat bordered").style("min-width: 320px"):
                        with ui.card_section():
                            with ui.row().classes("items-center gap-2"):
                                ui.icon("description", size="sm").style(f"color: {COLORS['ink2']}")
                                ui.label("Fiche de production").classes("text-subtitle1").style(
                                    f"color: {COLORS['ink']}; font-weight: 600"
                                )

                        with ui.card_section().classes("q-pt-none"):
                            sp_prev = app.storage.user.get("saved_production")
                            default_debut = (
                                _dt.date.fromisoformat(sp_prev["semaine_du"])
                                if sp_prev and "semaine_du" in sp_prev
                                else _dt.date.today()
                            )

                            # ── Date début fermentation ──
                            date_debut = date_picker_field(
                                default_debut.isoformat(),
                                label="Date début fermentation",
                            )

                            def do_save():
                                sd = date_debut.value
                                if isinstance(sd, str):
                                    sd_date = _dt.date.fromisoformat(sd)
                                else:
                                    sd_date = sd
                                ddm_date = sd_date + _dt.timedelta(days=DDM_DAYS)

                                g_order = []
                                if isinstance(df_min_override, pd.DataFrame) and "GoutCanon" in df_min_override.columns:
                                    for g in df_min_override["GoutCanon"].astype(str).tolist():
                                        if g and g not in g_order:
                                            g_order.append(g)

                                app.storage.user["saved_production"] = {
                                    "df_min_json": store_df(df_min_override),
                                    "df_calc_json": store_df(df_calc),
                                    "gouts": g_order,
                                    "semaine_du": sd_date.isoformat(),
                                    "ddm": ddm_date.isoformat(),
                                    "volume_details": {
                                        k: {kk: vv for kk, vv in v.items() if kk != "dilution_ingredients" or isinstance(vv, (dict, type(None)))}
                                        for k, v in volume_details.items()
                                    },
                                    "mode_prod": mode_prod,
                                }
                                # Audit trail
                                try:
                                    from common.audit import ACTION_PRODUCTION_SAVED, log_event
                                    log_event(
                                        tenant_id=app.storage.user.get("tenant_id"),
                                        user_email=app.storage.user.get("email"),
                                        action=ACTION_PRODUCTION_SAVED,
                                        details={"gouts": g_order, "semaine_du": sd_date.isoformat(), "mode": mode_prod},
                                    )
                                except Exception:
                                    _log.debug("Audit log_event production_saved failed", exc_info=True)
                                ui.notify("Production sauvegardée !", type="positive", icon="check")

                            # ── Checkbox téléchargement (précochée) ──
                            cb_download = ui.checkbox(
                                "Télécharger la fiche Excel",
                                value=True,
                            ).classes("q-mt-sm").props("dense color=green-8")

                            def _two_gouts(sp_obj):
                                g_saved = sp_obj.get("gouts", [])
                                uniq = []
                                for g in g_saved:
                                    if g and g not in uniq:
                                        uniq.append(g)
                                return (uniq + [None, None])[:2]

                            def _download_xlsx():
                                """Génère et télécharge la fiche Excel."""
                                try:
                                    _sp = app.storage.user.get("saved_production", {})
                                    _df_min_dl = load_df(_sp["df_min_json"])
                                    _df_calc_dl = load_df(_sp["df_calc_json"])
                                    _semaine = _dt.date.fromisoformat(_sp["semaine_du"])
                                    _ddm = _dt.date.fromisoformat(_sp["ddm"])
                                    _g1, _g2 = _two_gouts(_sp)
                                    _vd = (_sp.get("volume_details") or {}).get(_g1, {})

                                    xlsx_bytes = fill_fiche_xlsx(
                                        template_path=TEMPLATE_PATH,
                                        semaine_du=_semaine,
                                        ddm=_ddm,
                                        gout1=_g1 or "",
                                        gout2=_g2,
                                        df_calc=_df_calc_dl,
                                        df_min=_df_min_dl,
                                        V_start=_vd.get("V_start", 0),
                                        tank_capacity=_vd.get("capacity", 7200),
                                        transfer_loss=_vd.get("transfer_loss", 400),
                                        aromatisation_volume=_vd.get("V_aroma", 0),
                                        is_infusion=_vd.get("is_infusion", False),
                                        dilution_ingredients=_vd.get("dilution_ingredients"),
                                    )
                                    fname = f"Fiche de production - {_g1 or 'Multi'} - {_semaine.strftime('%d-%m-%Y')}.xlsx"
                                    ui.download(xlsx_bytes, fname)
                                    ui.notify("Fiche Excel générée !", type="positive")
                                except Exception as exc:
                                    ui.notify(f"Erreur Excel : {exc}", type="negative")

                            def do_save_and_download():
                                do_save()
                                ui.run_javascript("window._fsProductionDirty = false;")
                                if cb_download.value:
                                    _download_xlsx()
                                # Rafraîchir la section EasyBeer (maintenant que saved_production existe)
                                eb_container.clear()
                                with eb_container:
                                    _render_easybeer_section(
                                        mode_prod, volume_details, volume_cible,
                                        TANK_CONFIGS, TEMPLATE_PATH, COLORS,
                                        on_recreate=do_compute,
                                        gouts_cibles=gouts_cibles,
                                    )

                            ui.button(
                                "Sauvegarder",
                                icon="save",
                                on_click=do_save_and_download,
                            ).classes("w-full q-mt-sm").props("color=green-8 unelevated")

                    # ── Colonne droite : Créer dans EasyBeer ────────────
                    with ui.card().classes("flex-1").props("flat bordered").style("min-width: 320px"):
                        with ui.card_section():
                            with ui.row().classes("items-center gap-2"):
                                ui.icon("cloud_upload", size="sm").style(f"color: {COLORS['ink2']}")
                                ui.label("Créer dans EasyBeer").classes("text-subtitle1").style(
                                    f"color: {COLORS['ink']}; font-weight: 600"
                                )

                        eb_container = ui.card_section().classes("q-pt-none")
                        with eb_container:
                            _render_easybeer_section(
                                mode_prod, volume_details, volume_cible,
                                TANK_CONFIGS, TEMPLATE_PATH, COLORS,
                                on_recreate=do_compute,
                                gouts_cibles=gouts_cibles,
                            )

        # ── Watchers sidebar ──────────────────────────────────────────
        async def _on_mode_change(e=None):
            _build_split_inputs()
            await do_compute()

        # ── Debounce 300ms pour les watchers sidebar (M15) ──────────
        _debounce_task: dict = {"task": None}

        async def _debounced_compute(_=None):
            """Debounce : annule le recalcul précédent, attend 300ms."""
            if _debounce_task["task"] is not None:
                _debounce_task["task"].cancel()

            async def _delayed():
                await asyncio.sleep(0.3)
                await do_compute()

            _debounce_task["task"] = asyncio.ensure_future(_delayed())

        mode.on_value_change(_on_mode_change)
        repartir_cb.on_value_change(_debounced_compute)
        excluded_gouts_sel.on_value_change(_debounced_compute)
        excluded_products_sel.on_value_change(_debounced_compute)
        forced_gouts_sel.on_value_change(_debounced_compute)

        # ── Rendu initial ─────────────────────────────────────────────
        _build_split_inputs()
        await do_compute()
