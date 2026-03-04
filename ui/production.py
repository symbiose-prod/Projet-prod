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
from ui.theme import COLORS, date_picker_field, kpi_card, page_layout, section_title

_log = logging.getLogger("ferment.production")

# ====== Constantes metier (chargées depuis config.yaml) ======
_biz = get_business_config()
DEFAULT_LOSS_LARGE = _biz["default_loss_large"]
DEFAULT_LOSS_SMALL = _biz["default_loss_small"]
DDM_DAYS = _biz["ddm_days"]

# ====== Configurations cuves ======
TANK_CONFIGS = {**_biz["tanks"], "Manuel": None}

TEMPLATE_PATH = "assets/Fiche_production.xlsx"


# ─── Page ───────────────────────────────────────────────────────────────────

@ui.page("/production")
async def page_production():
    user = require_auth()
    if not user:
        return

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

            # Inputs mode Manuel (visibles seulement en Manuel)
            manual_container = ui.column().classes("w-full")

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

        # State pour les inputs mode Manuel
        volume_input_ref = {"ref": None}
        nb_gouts_input_ref = {"ref": None}

        def _build_manual_inputs():
            manual_container.clear()
            if mode.value == "Manuel":
                with manual_container:
                    volume_input_ref["ref"] = ui.number(
                        "Volume cible (hL)", value=64.0, min=1.0, max=1000.0, step=1.0,
                    ).props("outlined dense").classes("w-full")
                    nb_gouts_input_ref["ref"] = ui.select(
                        {1: "1 goût", 2: "2 goûts"},
                        value=1,
                        label="Nb goûts",
                    ).props("outlined dense").classes("w-full")

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

            if mode_prod == "Manuel":
                vol_ref = volume_input_ref["ref"]
                nb_ref = nb_gouts_input_ref["ref"]
                try:
                    volume_cible = float(vol_ref.value) if vol_ref else 64.0
                except (TypeError, ValueError):
                    volume_cible = 64.0
                try:
                    nb_gouts = int(nb_ref.value) if nb_ref else 1
                except (TypeError, ValueError):
                    nb_gouts = 1
            else:
                _tank = TANK_CONFIGS[mode_prod]
                nb_gouts = _tank["nb_gouts"]
                volume_cible = _tank["nominal_hL"]

            effective_nb_gouts = max(nb_gouts, len(forced_gouts)) if forced_gouts else nb_gouts

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
                    ),
                    timeout=60,
                )
            except TimeoutError:
                main_container.clear()
                with main_container:
                    ui.label("Le calcul a dépassé le délai (60 s). Réessayez avec moins de goûts ou un volume plus petit.").classes("text-negative")
                return
            except Exception as exc:
                main_container.clear()
                with main_container:
                    ui.label(f"Erreur optimiseur : {exc}").classes("text-negative")
                return

            df_min = _result["df_min"]
            gouts_cibles = _result["gouts_cibles"]
            df_calc = _result["df_calc"]
            df_all = _result["df_all"]
            note_msg = _result["note_msg"]
            volume_details = _result["volume_details"]
            volume_cible = _result["volume_cible"]
            df_final = _result["df_final"]

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
                                    <q-input
                                        v-model.number="props.row.forcer"
                                        type="number"
                                        dense
                                        borderless
                                        placeholder="auto"
                                        min="0"
                                        input-class="text-right text-bold"
                                        :input-style="{color: props.row.forcer != null ? '#F97316' : '#9CA3AF'}"
                                        style="max-width: 80px"
                                        :rules="[v => v == null || v === '' || v >= 0 || 'Min 0']"
                                    />
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

                    with ui.row().classes("w-full gap-3 q-mt-sm"):
                        async def do_apply_overrides():
                            """Lit les valeurs 'Forcer' depuis le tableau et recalcule."""
                            new_ov = {}
                            for r in table.rows:
                                if r.get("_sep"):
                                    continue
                                v = r.get("forcer")
                                if v is not None and v != "" and v != 0:
                                    try:
                                        vi = int(float(v))
                                        if vi >= 0:
                                            new_ov[r["_key"]] = vi
                                    except (TypeError, ValueError):
                                        pass
                            overrides.clear()
                            overrides.update(new_ov)
                            app.storage.user["production_overrides"] = dict(overrides)
                            await do_compute()

                        ui.button(
                            "Appliquer les forcés",
                            icon="check",
                            on_click=do_apply_overrides,
                        ).props("outline color=green-8")

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
                                if cb_download.value:
                                    _download_xlsx()

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

                        with ui.card_section().classes("q-pt-none"):
                            _render_easybeer_section(
                                mode_prod, volume_details, volume_cible,
                                TANK_CONFIGS, TEMPLATE_PATH, COLORS,
                                on_recreate=do_compute,
                            )

        # ── Watchers sidebar ──────────────────────────────────────────
        async def _on_mode_change(e=None):
            _build_manual_inputs()
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
        _build_manual_inputs()
        await do_compute()
