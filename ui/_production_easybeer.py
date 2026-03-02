"""
ui/_production_easybeer.py
==========================
Section EasyBeer de la page Production (création de brassins).

Extrait de ui/production.py pour maintenabilité.
"""
from __future__ import annotations

import asyncio
import logging
import re
import datetime as _dt

import pandas as pd
from nicegui import ui, app

from common.session_store import load_df
from common.xlsx_fill import fill_fiche_xlsx
from core.optimizer import parse_stock as _parse_stock
from ui._production_calc import _fetch_eb_products, _auto_match

_log = logging.getLogger("ferment.production")

# Constantes importées à la demande depuis production.py pour éviter
# une dépendance circulaire au niveau module.
DEFAULT_LOSS_LARGE = 800
DEFAULT_LOSS_SMALL = 400


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
                _df_calc_eb_parsed = load_df(_df_calc_eb_json)
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
        _log.debug("Erreur chargement materiels EasyBeer", exc_info=True)

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
                _log.debug("Erreur chargement entrepôts EasyBeer", exc_info=True)

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
                    _log.exception("Échec création brassin %s", g)
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
                        _df_min_eb = load_df(_df_min_eb_json)
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

                # Pause avant upload pour éviter le rate-limit EasyBeer (HTTP 429)
                await asyncio.sleep(2)

                # Upload fiche Excel
                try:
                    _semaine_dt = _dt.date.fromisoformat(_semaine_du_eb)
                    _ddm_dt = _dt.date.fromisoformat(_sp_eb.get("ddm", ""))
                    _sp_vd_eb = _sp_eb.get("volume_details") or {}
                    _vd_eb = _sp_vd_eb.get(g, {})
                    _df_min_dl = load_df(_sp_eb["df_min_json"])
                    _df_calc_dl = load_df(_sp_eb["df_calc_json"])
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
                    _log.exception("Échec upload fiche %s (brassin %s)", g, brassin_id)
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
