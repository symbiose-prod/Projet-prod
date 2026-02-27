"""
ui/production.py
================
Page Production — Planning et création brassins.

Réutilise toute la logique métier de core/optimizer.py, common/easybeer.py,
common/xlsx_fill.py. Seule la couche UI (NiceGUI) est spécifique ici.
"""
from __future__ import annotations

import asyncio
import os
import re
import time as _time
import datetime as _dt
from dateutil.relativedelta import relativedelta

import numpy as np
import pandas as pd
from nicegui import ui, app

from ui.auth import require_auth
from ui.theme import page_layout, kpi_card, section_title, COLORS
from ui.accueil import get_df_raw

from common.data import get_paths
from core.optimizer import (
    load_flavor_map_from_path,
    apply_canonical_flavor,
    sanitize_gouts,
    compute_plan,
    parse_stock as _parse_stock,
)
from common.xlsx_fill import fill_fiche_xlsx

# ====== Constantes metier ======
DEFAULT_LOSS_LARGE = 800   # perte totale (transfert+embouteillage) cuve 7200L
DEFAULT_LOSS_SMALL = 400   # perte totale cuve 5200L
DDM_DAYS = 365             # duree de vie par defaut (jours)

# ====== Configurations cuves ======
TANK_CONFIGS = {
    "Cuve de 7200L (1 goût)": {
        "capacity": 7200, "transfer_loss": 400, "bottling_loss": 400,
        "nb_gouts": 1, "nominal_hL": 64.0,
    },
    "Cuve de 5200L (1 goût)": {
        "capacity": 5200, "transfer_loss": 200, "bottling_loss": 200,
        "nb_gouts": 1, "nominal_hL": 48.0,
    },
    "Manuel": None,
}

TEMPLATE_PATH = "assets/Fiche_production.xlsx"


# ─── Helpers ────────────────────────────────────────────────────────────────

_EB_PRODUCTS_CACHE: dict = {"data": None, "ts": 0.0}
_EB_PRODUCTS_TTL = 300  # 5 minutes


def _fetch_eb_products() -> list[dict]:
    """Produits EasyBeer avec cache TTL 5 min (évite 3 appels HTTP par calcul).

    Ne met PAS en cache les échecs (exception ou liste vide) pour que le
    prochain appel retente l'API au lieu de servir un résultat vide pendant 5 min.
    """
    now = _time.monotonic()
    if _EB_PRODUCTS_CACHE["data"] is not None and (now - _EB_PRODUCTS_CACHE["ts"]) < _EB_PRODUCTS_TTL:
        return _EB_PRODUCTS_CACHE["data"]
    from common.easybeer import get_all_products
    data = get_all_products()
    if data:  # ne cache que les résultats non-vides
        _EB_PRODUCTS_CACHE["data"] = data
        _EB_PRODUCTS_CACHE["ts"] = now
    return data


def _auto_match(gout: str, prod_labels: list[str]) -> int:
    """Retourne l'index du produit EasyBeer dont le libellé contient le goût."""
    g_low = gout.lower()
    for i, lbl in enumerate(prod_labels):
        if g_low in lbl.lower():
            return i
    return 0


def _render_easybeer_section(
    mode_prod: str,
    volume_details: dict,
    volume_cible: float,
    tank_configs: dict,
    template_path: str,
    colors: dict,
    on_recreate=None,
):
    """Affiche le contenu de la carte EasyBeer (création de brassins)."""
    from common.easybeer import is_configured as _eb_configured

    if not _eb_configured():
        ui.label(
            "EasyBeer non configuré (EASYBEER_API_USER / EASYBEER_API_PASS manquants)."
        ).classes("text-caption text-grey-6")
        return

    if not app.storage.user.get("saved_production"):
        ui.label(
            "Sauvegarde d'abord une production ci-dessus."
        ).classes("text-caption text-grey-6")
        return

    _sp_eb = app.storage.user.get("saved_production", {})
    _gouts_eb = _sp_eb.get("gouts", [])

    if not _gouts_eb:
        ui.label("Aucun goût dans la production sauvegardée.").classes("text-caption text-grey-6")
        return

    _df_calc_eb_json = _sp_eb.get("df_calc_json")
    _semaine_du_eb = _sp_eb.get("semaine_du", "")
    _nb_gouts_eb = len(_gouts_eb)
    _perte_litres = DEFAULT_LOSS_LARGE

    # ── Volume par gout ──────────────────────────────────────────
    _vol_par_gout: dict[str, float] = {}

    if mode_prod != "Manuel" and volume_details:
        for g in _gouts_eb:
            if g in volume_details:
                _vol_par_gout[g] = volume_details[g]["V_start"]
            else:
                _tank_eb = tank_configs.get(mode_prod) or tank_configs["Cuve de 7200L (1 goût)"]
                _vol_par_gout[g] = float(_tank_eb["capacity"])
        _perte_litres = tank_configs[mode_prod]["transfer_loss"] + tank_configs[mode_prod]["bottling_loss"]
    else:
        _perte_litres = DEFAULT_LOSS_LARGE if volume_cible > 50 else DEFAULT_LOSS_SMALL
        if _nb_gouts_eb == 1:
            _vol_par_gout[_gouts_eb[0]] = volume_cible * 100 + _perte_litres
        else:
            if _df_calc_eb_json:
                _df_calc_eb_parsed = pd.read_json(_df_calc_eb_json, orient="split")
                _proportions = {}
                _total_x = 0.0
                if "X_adj (hL)" in _df_calc_eb_parsed.columns:
                    for g in _gouts_eb:
                        mask = _df_calc_eb_parsed["GoutCanon"].astype(str) == g
                        val = float(_df_calc_eb_parsed.loc[mask, "X_adj (hL)"].sum())
                        _proportions[g] = val
                        _total_x += val
                if _total_x > 0:
                    for g in _gouts_eb:
                        part = (_proportions.get(g, 0) / _total_x) * volume_cible
                        _vol_par_gout[g] = part * 100 + _perte_litres
                else:
                    for g in _gouts_eb:
                        _vol_par_gout[g] = (volume_cible / _nb_gouts_eb) * 100 + _perte_litres
            else:
                for g in _gouts_eb:
                    _vol_par_gout[g] = (volume_cible / _nb_gouts_eb) * 100 + _perte_litres

    # ── Charger les produits EasyBeer ────────────────────────────
    try:
        _eb_products = _fetch_eb_products()
    except Exception as exc:
        ui.notify(f"Erreur EasyBeer : {exc}", type="negative")
        _eb_products = []

    if not _eb_products:
        ui.label("Aucun produit trouvé dans EasyBeer.").classes("text-caption text-grey-6")
        return

    _prod_labels = [p.get("libelle", "") for p in _eb_products]

    # ── Résumé : date de début + goûts ───────────────────────
    _date_debut_fmt = _dt.date.fromisoformat(_semaine_du_eb).strftime("%d/%m/%Y")
    with ui.row().classes("w-full gap-6 q-mb-sm"):
        with ui.column().classes("gap-0"):
            ui.label("Début fermentation").classes("text-caption text-grey-6")
            ui.label(_date_debut_fmt).classes("text-subtitle1").style(
                f"color: {colors['ink']}; font-weight: 700"
            )
        with ui.column().classes("gap-0"):
            ui.label("Goût(s)").classes("text-caption text-grey-6")
            ui.label(", ".join(_gouts_eb)).classes("text-subtitle1").style(
                f"color: {colors['green']}; font-weight: 700"
            )

    ui.separator().classes("q-my-xs")

    # ── Date d'embouteillage ──────────────────────────────────
    ui.label("Date embouteillage").classes(
        "text-subtitle2 q-mb-xs"
    ).style(f"color: {colors['ink']}; font-weight: 600")

    _default_embout = _dt.date.fromisoformat(_semaine_du_eb) + _dt.timedelta(days=7)
    date_embout_input = ui.input(
        value=_default_embout.isoformat(),
    ).props("outlined dense").classes("w-full")
    with date_embout_input:
        with ui.menu().props("no-parent-event") as embout_menu:
            embout_picker = ui.date(value=_default_embout.isoformat()).props("dense first-day-of-week=1")
            embout_picker.on_value_change(
                lambda e: (date_embout_input.set_value(e.value), embout_menu.close())
            )
        with date_embout_input.add_slot("append"):
            ui.icon("event", size="xs").classes("cursor-pointer").on(
                "click", lambda: embout_menu.open()
            )

    # ── Produit auto-matché par goût (affiché en lecture seule) ─
    _product_indices: dict[str, int] = {}
    for g in _gouts_eb:
        vol_l = _vol_par_gout.get(g, 0)
        matched_idx = _auto_match(g, _prod_labels)
        _product_indices[g] = matched_idx
        matched_label = _prod_labels[matched_idx] if _prod_labels else "—"
        with ui.row().classes("w-full items-center gap-3 q-mt-xs"):
            with ui.column().classes("gap-0"):
                ui.label(g).classes("text-subtitle2").style(f"color: {colors['ink']}; font-weight: 600")
                ui.label(f"{vol_l:.0f} L").classes("text-caption text-grey-6")
            ui.label(f"→ {matched_label}").classes("text-body2 text-grey-7")

    # ── Sélection cuves ──────────────────────────────────────────
    from common.easybeer import get_all_materiels
    _materiels: list[dict] = []
    try:
        _materiels = get_all_materiels()
    except Exception:
        pass

    _tank_cap_eb = 0
    if mode_prod != "Manuel" and volume_details:
        _vd_first = list(volume_details.values())[0]
        _tank_cap_eb = _vd_first.get("capacity", 0)
    elif mode_prod != "Manuel":
        _tank_cfg_eb = tank_configs.get(mode_prod) or {}
        _tank_cap_eb = _tank_cfg_eb.get("capacity", 0) if _tank_cfg_eb else 0

    _cuves_fermentation = [
        m for m in _materiels
        if m.get("type", {}).get("code") == "CUVE_FERMENTATION"
        and abs(m.get("volume", 0) - _tank_cap_eb) < 100
    ] if _tank_cap_eb > 0 else []

    _cuve_dilution = next(
        (m for m in _materiels if m.get("type", {}).get("code") == "CUVE_FABRICATION"),
        None,
    )

    cuve_a_sel = None
    cuve_b_sel = None

    if _cuves_fermentation:
        ui.separator().classes("q-my-sm")
        ui.label("Affectation des cuves").classes("text-subtitle2")

        _cuve_options = {
            i: f"{m.get('identifiant', '')} ({m.get('volume', 0):.0f}L) — {m.get('etatCourant', {}).get('libelle', '?')}"
            for i, m in enumerate(_cuves_fermentation)
        }

        with ui.row().classes("w-full gap-4"):
            cuve_a_sel = ui.select(
                _cuve_options, value=0,
                label="Cuve fermentation (A)",
            ).props("outlined dense").classes("flex-1")

            _default_b = 1 if len(_cuves_fermentation) > 1 else 0
            cuve_b_sel = ui.select(
                _cuve_options, value=_default_b,
                label="Cuve garde (B)",
            ).props("outlined dense").classes("flex-1")

    # ── Guard anti-doublon ───────────────────────────────────────
    _creation_key = f"{_semaine_du_eb}_{'_'.join(_gouts_eb)}"
    _already_created = app.storage.user.get("_eb_brassins_created", {})

    if _creation_key in _already_created:
        ids = _already_created[_creation_key]
        ui.label(
            f"✅ Brassins déjà créés (IDs : {', '.join(str(i) for i in ids)})."
        ).classes("text-positive text-body2 q-mt-sm")

        async def do_recreate():
            del app.storage.user["_eb_brassins_created"][_creation_key]
            if on_recreate:
                _r = on_recreate()
                if asyncio.iscoroutine(_r):
                    await _r

        ui.button("Recréer", icon="refresh", on_click=do_recreate).props("flat color=grey-7")
    else:
        async def do_create_brassins():
            from common.easybeer import (
                create_brassin, get_product_detail, get_warehouses,
                get_planification_matrice, add_planification_conditionnement,
                upload_fichier_brassin,
            )
            import unicodedata as _ud_etape

            def _norm_etape(s: str) -> str:
                s = _ud_etape.normalize("NFKD", s)
                s = "".join(ch for ch in s if not _ud_etape.combining(ch))
                return s.lower()

            # Entrepôt principal
            _id_entrepot = None
            try:
                _warehouses = get_warehouses()
                for _w in _warehouses:
                    if _w.get("principal"):
                        _id_entrepot = _w.get("idEntrepot")
                        break
                if not _id_entrepot and _warehouses:
                    _id_entrepot = _warehouses[0].get("idEntrepot")
            except Exception:
                pass

            _selected_cuve_a_id = (
                _cuves_fermentation[cuve_a_sel.value].get("idMateriel")
                if cuve_a_sel and _cuves_fermentation else None
            )
            _selected_cuve_b_id = (
                _cuves_fermentation[cuve_b_sel.value].get("idMateriel")
                if cuve_b_sel and _cuves_fermentation else None
            )

            created_ids = []
            errors = []

            # Tracker FIFO des lots — persiste entre goûts pour la conso virtuelle
            from common.lot_fifo import BatchLotTracker
            from common.easybeer import get_mp_lots
            _lot_tracker = BatchLotTracker(fetch_lots_fn=get_mp_lots)

            for g in _gouts_eb:
                vol_l = _vol_par_gout.get(g, 0)
                _sel_idx = _product_indices[g]
                id_produit = _eb_products[_sel_idx]["idProduit"]

                # Nom du brassin
                _date_obj = _dt.date.fromisoformat(_semaine_du_eb)
                _prod_label = _eb_products[_sel_idx].get("libelle", "")
                if "infusion" in _prod_label.lower():
                    _code = "IP" + g[:1].upper() + _date_obj.strftime("%d%m%Y")
                else:
                    _code = "K" + g[:2].upper() + _date_obj.strftime("%d%m%Y")

                # Recette + étapes
                _ingredients = []
                _planif_etapes = []
                try:
                    prod_detail = get_product_detail(id_produit)
                    recettes = prod_detail.get("recettes") or []
                    etapes = prod_detail.get("etapes") or []

                    if recettes:
                        recette = recettes[0]
                        vol_recette = recette.get("volumeRecette", 0)
                        ratio = vol_l / vol_recette if vol_recette > 0 else 1
                        for ing in recette.get("ingredients") or []:
                            base_ing = {
                                "idProduitIngredient": ing.get("idProduitIngredient"),
                                "matierePremiere": ing.get("matierePremiere"),
                                "quantite": round(ing.get("quantite", 0) * ratio, 2),
                                "ordre": ing.get("ordre", 0),
                                "unite": ing.get("unite"),
                                "brassageEtape": ing.get("brassageEtape"),
                                "modeleNumerosLots": [],
                            }
                            # Distribution FIFO des lots
                            _ingredients.extend(
                                _lot_tracker.distribute_ingredient(base_ing)
                            )

                    for et in etapes:
                        _etape_nom = _norm_etape(
                            (et.get("brassageEtape") or {}).get("nom", "")
                        )
                        _mat = {}
                        if _selected_cuve_a_id and (
                            "fermentation" in _etape_nom
                            or "aromatisation" in _etape_nom
                            or "filtration" in _etape_nom
                        ):
                            _mat = {"idMateriel": _selected_cuve_a_id}
                        elif _selected_cuve_b_id and (
                            "transfert" in _etape_nom or "garde" in _etape_nom
                        ):
                            _mat = {"idMateriel": _selected_cuve_b_id}
                        elif _cuve_dilution and (
                            "preparation" in _etape_nom or "sirop" in _etape_nom
                        ):
                            _mat = {"idMateriel": _cuve_dilution.get("idMateriel")}

                        _planif_etapes.append({
                            "produitEtape": {
                                "idProduitEtape": et.get("idProduitEtape"),
                                "brassageEtape": et.get("brassageEtape"),
                                "ordre": et.get("ordre"),
                                "duree": et.get("duree"),
                                "unite": et.get("unite"),
                                "etapeTerminee": False,
                                "etapeEnCours": False,
                            },
                            "materiel": _mat,
                        })
                except Exception as exc:
                    ui.notify(f"Recette « {g} » : {exc}", type="warning")

                # Date embouteillage
                _date_embout = date_embout_input.value
                if isinstance(_date_embout, str):
                    _date_embout_iso = _date_embout
                else:
                    _date_embout_iso = _date_embout.isoformat()

                payload = {
                    "nom": _code,
                    "volume": round(vol_l, 1),
                    "pourcentagePerte": round(_perte_litres / vol_l * 100, 2) if vol_l > 0 else 0,
                    "dateDebutFormulaire": f"{_semaine_du_eb}T07:30:00.000Z",
                    "dateConditionnementPrevue": f"{_date_embout_iso}T23:00:00.000Z",
                    "produit": {"idProduit": id_produit},
                    "type": {"code": "LOCALE"},
                    "deduireMatierePremiere": True,
                    "changementEtapeAutomatique": True,
                }
                if _ingredients:
                    payload["ingredients"] = _ingredients
                if _planif_etapes:
                    payload["planificationsEtapes"] = _planif_etapes

                try:
                    result = create_brassin(payload)
                    brassin_id = result.get("id", "?")
                    created_ids.append(brassin_id)
                    ui.notify(f"Brassin « {g} » créé (ID {brassin_id})", type="positive")
                except Exception as exc:
                    errors.append(f"{g} : {exc}")
                    continue

                # Planification conditionnement
                if not isinstance(brassin_id, int) or not _id_entrepot:
                    continue

                try:
                    _matrice = get_planification_matrice(brassin_id, _id_entrepot)
                    _cont_by_vol: dict[float, list[dict]] = {}
                    for _mc in _matrice.get("contenants", []):
                        _mod = _mc.get("modeleContenant", {})
                        _cap = _mod.get("contenance")
                        if _cap is not None:
                            _cont_by_vol.setdefault(round(float(_cap), 2), []).append(_mod)

                    _pkg_lookup: dict[str, int] = {}
                    for _pk in _matrice.get("packagings", []):
                        _lbl = (_pk.get("libelle") or "").strip().lower()
                        if _lbl and _pk.get("idLot") is not None:
                            _pkg_lookup[_lbl] = _pk["idLot"]

                    # ── Produits dérivés (NIKO, INTER…) ──
                    _derive_map: dict[str, int] = {}
                    for _d in _matrice.get("produitsDerives", []):
                        _d_lbl = (_d.get("libelle") or "").lower()
                        _d_id = _d.get("idProduit")
                        if not _d_id:
                            continue
                        if "niko" in _d_lbl:
                            _derive_map["niko"] = _d_id
                        elif "inter" in _d_lbl:
                            _derive_map["inter"] = _d_id
                        elif "water" in _d_lbl:
                            _derive_map["water"] = _d_id

                    def _product_id_for_line(produit_str: str) -> int:
                        """Retourne l'idProduit du dérivé si NIKO/INTER, sinon le principal."""
                        p = produit_str.lower()
                        for kw, pid in _derive_map.items():
                            if kw in p:
                                return pid
                        return id_produit

                    # ── Construire les éléments groupés par produit ──
                    _elements_by_pid: dict[int, list[dict]] = {}
                    _df_min_eb_json = _sp_eb.get("df_min_json")
                    if _df_min_eb_json:
                        _df_min_eb = pd.read_json(_df_min_eb_json, orient="split")
                        _rows_gout = _df_min_eb[_df_min_eb["GoutCanon"].astype(str) == g]
                        for _, _r in _rows_gout.iterrows():
                            _stock = str(_r.get("Stock", "")).strip()
                            _produit_col = str(_r.get("Produit", "")).strip()
                            _ct = int(_r.get("Cartons à produire (arrondi)", 0))
                            if _ct <= 0:
                                continue

                            _pkg_m = re.search(
                                r"((?:carton|pack|caisse|colis)\s+de\s+\d+)",
                                _stock, re.IGNORECASE,
                            )
                            _pkg_name = _pkg_m.group(1).strip().lower() if _pkg_m else ""
                            _id_lot = None
                            for _pk_lbl, _pk_id in _pkg_lookup.items():
                                if _pkg_name and _pkg_name in _pk_lbl:
                                    _id_lot = _pk_id
                                    break

                            _, _vol_btl = _parse_stock(_stock)
                            _id_cont = None
                            if _vol_btl is not None and not pd.isna(_vol_btl):
                                _vol_key = round(float(_vol_btl), 2)
                                _candidates = _cont_by_vol.get(_vol_key, [])
                                if len(_candidates) == 1:
                                    _id_cont = _candidates[0].get("idContenant")
                                elif len(_candidates) > 1:
                                    _is_pack = "pack" in _pkg_name
                                    for _c in _candidates:
                                        _c_lbl = (_c.get("libelleAvecContenance") or _c.get("libelle") or "").lower()
                                        if _is_pack and "saft" in _c_lbl:
                                            _id_cont = _c.get("idContenant")
                                            break
                                        elif not _is_pack and "saft" not in _c_lbl:
                                            _id_cont = _c.get("idContenant")
                                            break
                                    if _id_cont is None:
                                        _id_cont = _candidates[0].get("idContenant")

                            if _id_cont is not None and _id_lot is not None:
                                _pid = _product_id_for_line(_produit_col)
                                _elements_by_pid.setdefault(_pid, []).append({
                                    "idContenant": _id_cont,
                                    "idLot": _id_lot,
                                    "quantite": _ct,
                                })

                    # ── Créer une planification par produit (principal + dérivés) ──
                    _ddm_iso = _sp_eb.get("ddm", "")
                    for _pid, _elems in _elements_by_pid.items():
                        add_planification_conditionnement({
                            "idBrassin": brassin_id,
                            "idProduit": _pid,
                            "idEntrepot": _id_entrepot,
                            "date": f"{_date_embout_iso}T23:00:00.000Z",
                            "dateLimiteUtilisationOptimale": f"{_ddm_iso}T00:00:00.000Z" if _ddm_iso else "",
                            "elements": _elems,
                        })
                    if _elements_by_pid:
                        _nb_planifs = len(_elements_by_pid)
                        ui.notify(
                            f"Conditionnement « {g} » planifié ({_nb_planifs} produit{'s' if _nb_planifs > 1 else ''})",
                            type="positive",
                        )
                except Exception as _pe:
                    ui.notify(f"Planif. « {g} » : {_pe}", type="warning")

                # Upload fiche Excel
                try:
                    _semaine_dt = _dt.date.fromisoformat(_semaine_du_eb)
                    _ddm_dt = _dt.date.fromisoformat(_sp_eb.get("ddm", ""))
                    _sp_vd_eb = _sp_eb.get("volume_details") or {}
                    _vd_eb = _sp_vd_eb.get(g, {})
                    _df_min_dl = pd.read_json(_sp_eb["df_min_json"], orient="split")
                    _df_calc_dl = pd.read_json(_sp_eb["df_calc_json"], orient="split")
                    _fiche_bytes = fill_fiche_xlsx(
                        template_path=template_path,
                        semaine_du=_semaine_dt,
                        ddm=_ddm_dt,
                        gout1=g,
                        gout2=None,
                        df_calc=_df_calc_dl,
                        df_min=_df_min_dl,
                        V_start=_vd_eb.get("V_start", 0),
                        tank_capacity=_vd_eb.get("capacity", 7200),
                        transfer_loss=_vd_eb.get("transfer_loss", 400),
                        aromatisation_volume=_vd_eb.get("V_aroma", 0),
                        is_infusion=_vd_eb.get("is_infusion", False),
                        dilution_ingredients=_vd_eb.get("dilution_ingredients"),
                    )
                    _fiche_name = f"Fiche de production — {g} — {_semaine_dt.strftime('%d-%m-%Y')}.xlsx"
                    upload_fichier_brassin(
                        id_brassin=brassin_id,
                        file_bytes=_fiche_bytes,
                        filename=_fiche_name,
                        commentaire=f"Fiche de production {g}",
                    )
                    ui.notify(f"Fiche « {g} » uploadée", type="positive")
                except Exception as _ue:
                    ui.notify(f"Upload fiche « {g} » : {_ue}", type="warning")

            # Résultat
            if created_ids:
                _created = app.storage.user.setdefault("_eb_brassins_created", {})
                _created[_creation_key] = created_ids
                ui.notify(
                    f"{len(created_ids)} brassin(s) créé(s) !",
                    type="positive", icon="check",
                )
            for err in errors:
                ui.notify(err, type="negative")

        # Dialogue de confirmation (action irréversible dans l'ERP)
        with ui.dialog() as _confirm_dlg, ui.card().classes("q-pa-lg"):
            ui.label("Confirmer la création des brassins ?").classes("text-subtitle1").style(
                f"color: {colors['ink']}; font-weight: 600"
            )
            ui.label(
                f"{len(_gouts_eb)} brassin(s) seront créés dans EasyBeer. "
                "Cette action est irréversible."
            ).classes("text-body2 text-grey-7 q-mt-xs")
            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Annuler", on_click=_confirm_dlg.close).props("flat color=grey-7")

                async def _confirmed_create():
                    _confirm_dlg.close()
                    await do_create_brassins()

                ui.button(
                    "Créer",
                    icon="rocket_launch",
                    on_click=_confirmed_create,
                ).props("color=green-8 unelevated")

        ui.button(
            "Créer les brassins",
            icon="rocket_launch",
            on_click=_confirm_dlg.open,
        ).classes("w-full q-mt-md").props("color=green-8 unelevated")


def _build_final_table(
    df_all: pd.DataFrame,
    df_calc: pd.DataFrame,
    gouts_cibles: list[str],
    overrides: dict,
) -> pd.DataFrame:
    """Construit le tableau avec tous les formats + overrides + redistribution."""
    sel = set(gouts_cibles)
    base = (
        df_all[df_all["GoutCanon"].isin(sel)][
            ["GoutCanon", "Produit", "Stock", "Volume/carton (hL)", "Bouteilles/carton"]
        ]
        .drop_duplicates(subset=["GoutCanon", "Produit", "Stock"])
        .copy()
        .reset_index(drop=True)
    )
    base = base.merge(
        df_calc[["GoutCanon", "Produit", "Stock", "X_adj (hL)"]],
        on=["GoutCanon", "Produit", "Stock"],
        how="left",
    )
    base["X_adj (hL)"] = base["X_adj (hL)"].fillna(0.0)

    rows_out = []
    for g, grp in base.groupby("GoutCanon", sort=False):
        V_g = grp["X_adj (hL)"].sum()
        forced_vol_g = 0.0
        for _, row in grp.iterrows():
            key = f"{row['GoutCanon']}|{row['Produit']}|{row['Stock']}"
            if key in overrides:
                forced_vol_g += overrides[key] * row["Volume/carton (hL)"]
        remaining_g = max(0.0, V_g - forced_vol_g)
        nf_weight = grp.loc[
            grp.apply(
                lambda r: f"{r['GoutCanon']}|{r['Produit']}|{r['Stock']}" not in overrides,
                axis=1,
            ),
            "X_adj (hL)",
        ].sum()

        for _, row in grp.iterrows():
            key = f"{row['GoutCanon']}|{row['Produit']}|{row['Stock']}"
            forced = overrides.get(key)
            if forced is not None:
                cartons = max(0, int(forced))
            else:
                if nf_weight > 1e-9 and row["X_adj (hL)"] > 0:
                    alloc_hl = remaining_g * row["X_adj (hL)"] / nf_weight
                    cartons = max(0, int(round(alloc_hl / row["Volume/carton (hL)"])))
                else:
                    cartons = 0
            bouteilles = int(cartons * row["Bouteilles/carton"])
            vol = round(cartons * row["Volume/carton (hL)"], 3)
            rows_out.append({
                "GoutCanon": row["GoutCanon"],
                "Produit": row["Produit"],
                "Stock": row["Stock"],
                "Volume/carton (hL)": row["Volume/carton (hL)"],
                "Bouteilles/carton": int(row["Bouteilles/carton"]),
                "Cartons à produire (arrondi)": cartons,
                "Bouteilles à produire (arrondi)": bouteilles,
                "Volume produit arrondi (hL)": vol,
                "_forcé": forced is not None,
            })
    return pd.DataFrame(rows_out) if rows_out else pd.DataFrame()


# ─── Calcul lourd (exécuté dans le thread pool) ─────────────────────────────

def _compute_production_sync(
    df_in_filtered: pd.DataFrame,
    window_days: int,
    volume_cible: float,
    effective_nb_gouts: int,
    repartir_pro_rv: bool,
    forced_gouts: list[str],
    excluded_gouts: list[str],
    mode_prod: str,
    overrides: dict,
) -> dict:
    """Passe 1 (optimiseur) + Passe 2 (EasyBeer) — aucun appel UI."""
    # ── PASSE 1 : Optimiseur
    (
        df_min, cap_resume, gouts_cibles, synth_sel,
        df_calc, df_all, note_msg,
    ) = compute_plan(
        df_in=df_in_filtered,
        window_days=window_days,
        volume_cible=volume_cible,
        nb_gouts=effective_nb_gouts,
        repartir_pro_rv=repartir_pro_rv,
        manual_keep=forced_gouts or None,
        exclude_list=excluded_gouts,
    )

    # ── PASSE 2 : Aromatisation (modes auto)
    volume_details: dict = {}
    if mode_prod != "Manuel" and gouts_cibles:
        from common.easybeer import (
            is_configured as _eb_conf_p2,
            compute_aromatisation_volume,
            compute_v_start_max,
            compute_dilution_ingredients,
        )
        _tank_cfg = TANK_CONFIGS[mode_prod]
        _C = _tank_cfg["capacity"]
        _Lt = _tank_cfg["transfer_loss"]
        _Lb = _tank_cfg["bottling_loss"]
        _gout_p2 = gouts_cibles[0]
        _A_R, _R = 0.0, 0.0
        _id_prod_p2 = None

        if _eb_conf_p2():
            try:
                _eb_prods_p2 = _fetch_eb_products()
                _labels_p2 = [p.get("libelle", "") for p in _eb_prods_p2]
                _matched_idx = _auto_match(_gout_p2, _labels_p2)
                _id_prod_p2 = _eb_prods_p2[_matched_idx]["idProduit"]
                _A_R, _R = compute_aromatisation_volume(_id_prod_p2)
            except Exception:
                _A_R, _R = 0.0, 0.0

        _V_start, _V_bottled = compute_v_start_max(_C, _Lt, _Lb, _A_R, _R)
        _volume_cible_recalc = _V_bottled / 100.0

        _is_infusion_p2 = False
        _dilution_p2: dict = {}
        if _id_prod_p2 is not None:
            try:
                _prod_label_p2 = _eb_prods_p2[_matched_idx].get("libelle", "")
                _is_infusion_p2 = (
                    "infusion" in _prod_label_p2.lower()
                    or _prod_label_p2.upper().startswith("EP")
                )
            except Exception:
                pass
            try:
                _dilution_p2 = compute_dilution_ingredients(_id_prod_p2, _V_start)
            except Exception:
                _dilution_p2 = {}

        volume_details[_gout_p2] = {
            "V_start": _V_start,
            "A_R": _A_R,
            "R": _R,
            "V_aroma": _A_R * (_V_start / _R) if _R > 0 else 0.0,
            "V_bottled": _V_bottled,
            "capacity": _C,
            "transfer_loss": _Lt,
            "bottling_loss": _Lb,
            "is_infusion": _is_infusion_p2,
            "dilution_ingredients": _dilution_p2,
            "id_produit": _id_prod_p2,
        }

        # Relance si le volume a changé
        if abs(_volume_cible_recalc - volume_cible) > 0.01:
            volume_cible = _volume_cible_recalc
            try:
                (
                    df_min, cap_resume, gouts_cibles, synth_sel,
                    df_calc, df_all, note_msg,
                ) = compute_plan(
                    df_in=df_in_filtered,
                    window_days=window_days,
                    volume_cible=volume_cible,
                    nb_gouts=effective_nb_gouts,
                    repartir_pro_rv=repartir_pro_rv,
                    manual_keep=forced_gouts or None,
                    exclude_list=excluded_gouts,
                )
            except Exception:
                import logging
                logging.getLogger("ferment.production").exception(
                    "Erreur recalcul passe 2 (volume ajusté %.2f hL)", volume_cible
                )

    df_final = _build_final_table(df_all, df_calc, gouts_cibles, overrides)

    return {
        "df_min": df_min,
        "cap_resume": cap_resume,
        "gouts_cibles": gouts_cibles,
        "synth_sel": synth_sel,
        "df_calc": df_calc,
        "df_all": df_all,
        "note_msg": note_msg,
        "volume_details": volume_details,
        "volume_cible": volume_cible,
        "df_final": df_final,
    }


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
                    ),
                    timeout=60,
                )
            except asyncio.TimeoutError:
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
                    pass

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

                            # ── Titre visible pour la date ──
                            ui.label("Date début fermentation").classes(
                                "text-subtitle2 q-mb-xs"
                            ).style(f"color: {COLORS['ink']}; font-weight: 600")

                            date_debut = ui.input(
                                value=default_debut.isoformat(),
                            ).props("outlined dense").classes("w-full")
                            with date_debut:
                                with ui.menu().props("no-parent-event") as date_menu:
                                    date_picker = ui.date(value=default_debut.isoformat()).props("dense first-day-of-week=1")
                                    date_picker.on_value_change(
                                        lambda e: (date_debut.set_value(e.value), date_menu.close())
                                    )
                                with date_debut.add_slot("append"):
                                    ui.icon("event", size="xs").classes("cursor-pointer").on(
                                        "click", lambda: date_menu.open()
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
                                    "df_min_json": df_min_override.to_json(orient="split"),
                                    "df_calc_json": df_calc.to_json(orient="split"),
                                    "gouts": g_order,
                                    "semaine_du": sd_date.isoformat(),
                                    "ddm": ddm_date.isoformat(),
                                    "volume_details": {
                                        k: {kk: vv for kk, vv in v.items() if kk != "dilution_ingredients" or isinstance(vv, (dict, type(None)))}
                                        for k, v in volume_details.items()
                                    },
                                    "mode_prod": mode_prod,
                                }
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
                                    _df_min_dl = pd.read_json(_sp["df_min_json"], orient="split")
                                    _df_calc_dl = pd.read_json(_sp["df_calc_json"], orient="split")
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
