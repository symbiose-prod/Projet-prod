"""
ui/_production_easybeer.py
==========================
Section EasyBeer de la page Production (création de brassins).

Extrait de ui/production.py pour maintenabilité.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re

import requests
from nicegui import app, ui

from common.easybeer import EasyBeerError
from common.session_store import load_df
from common.xlsx_fill import fill_fiche_xlsx
from core.optimizer import parse_stock as _parse_stock
from ui._production_calc import _auto_match, _fetch_eb_products
from ui.theme import confirm_dialog, date_picker_field

_log = logging.getLogger("ferment.production")

# Constantes métier chargées depuis config.yaml (source unique de vérité)
from common.data import get_business_config as _get_biz

_biz_eb = _get_biz()
DEFAULT_LOSS_LARGE = _biz_eb["default_loss_large"]
DEFAULT_LOSS_SMALL = _biz_eb["default_loss_small"]


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
    except (EasyBeerError, requests.RequestException) as exc:
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
    _default_embout = _dt.date.fromisoformat(_semaine_du_eb) + _dt.timedelta(days=7)
    date_embout_input = date_picker_field(
        _default_embout.isoformat(),
        label="Date embouteillage",
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
    except (EasyBeerError, requests.RequestException):
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
            i: (
                f"{m.get('identifiant', '')} ({m.get('volume', 0):.0f}L)"
                f" — {m.get('etatCourant', {}).get('libelle', '?')}"
            )
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
            from common.brassin_builder import (
                build_brassin_payload,
                build_etape_planification,
                generate_brassin_code,
                match_contenant_id,
                parse_derive_map,
                parse_packaging_lookup,
                scale_recipe_ingredients,
            )
            from common.easybeer import (
                add_planification_conditionnement,
                create_brassin,
                get_planification_matrice,
                get_product_detail,
                get_warehouses,
                upload_fichier_brassin,
            )

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
            except (EasyBeerError, requests.RequestException):
                _log.debug("Erreur chargement entrepôts EasyBeer", exc_info=True)

            _selected_cuve_a_id = (
                _cuves_fermentation[cuve_a_sel.value].get("idMateriel")
                if cuve_a_sel and _cuves_fermentation else None
            )
            _selected_cuve_b_id = (
                _cuves_fermentation[cuve_b_sel.value].get("idMateriel")
                if cuve_b_sel and _cuves_fermentation else None
            )
            _cuve_dilution_id = _cuve_dilution.get("idMateriel") if _cuve_dilution else None

            created_ids = []
            errors = []

            # Tracker FIFO des lots — persiste entre goûts pour la conso virtuelle
            from common.easybeer import get_mp_lots
            from common.lot_fifo import BatchLotTracker
            _lot_tracker = BatchLotTracker(fetch_lots_fn=get_mp_lots)

            for g in _gouts_eb:
                vol_l = _vol_par_gout.get(g, 0)
                _sel_idx = _product_indices[g]
                id_produit = _eb_products[_sel_idx]["idProduit"]
                _prod_label = _eb_products[_sel_idx].get("libelle", "")

                _code = generate_brassin_code(g, _semaine_du_eb, _prod_label)

                # Recette + étapes (via brassin_builder)
                _ingredients = []
                _planif_etapes = []
                try:
                    prod_detail = get_product_detail(id_produit)
                    recettes = prod_detail.get("recettes") or []
                    etapes = prod_detail.get("etapes") or []

                    if recettes:
                        base_ings = scale_recipe_ingredients(recettes[0], vol_l)
                        for base_ing in base_ings:
                            _ingredients.extend(
                                _lot_tracker.distribute_ingredient(base_ing)
                            )

                    _planif_etapes = build_etape_planification(
                        etapes, _selected_cuve_a_id, _selected_cuve_b_id, _cuve_dilution_id,
                    )
                except (EasyBeerError, requests.RequestException, KeyError, ValueError) as exc:
                    ui.notify(f"Recette « {g} » : {exc}", type="warning")

                # Date embouteillage
                _date_embout = date_embout_input.value
                _date_embout_iso = _date_embout if isinstance(_date_embout, str) else _date_embout.isoformat()

                payload = build_brassin_payload(
                    code=_code,
                    vol_l=vol_l,
                    perte_litres=_perte_litres,
                    semaine_du=_semaine_du_eb,
                    date_embout_iso=_date_embout_iso,
                    id_produit=id_produit,
                    ingredients=_ingredients,
                    planif_etapes=_planif_etapes,
                )

                try:
                    result = create_brassin(payload)
                    brassin_id = result.get("id", "?")
                    created_ids.append(brassin_id)
                    ui.notify(f"Brassin « {g} » créé (ID {brassin_id})", type="positive")
                except (EasyBeerError, requests.RequestException) as exc:
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

                    _pkg_lookup = parse_packaging_lookup(_matrice)
                    _derive_map = parse_derive_map(_matrice)

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
                            _id_cont = match_contenant_id(_stock, _vol_btl, _cont_by_vol)

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
                except (EasyBeerError, requests.RequestException, KeyError, ValueError) as _pe:
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
                except (EasyBeerError, requests.RequestException, OSError, KeyError, ValueError) as _ue:
                    _log.exception("Échec upload fiche %s (brassin %s)", g, brassin_id)
                    ui.notify(f"Upload fiche « {g} » : {_ue}", type="warning")

            # Résultat
            if created_ids:
                _created = app.storage.user.setdefault("_eb_brassins_created", {})
                _created[_creation_key] = created_ids
                # Audit trail
                try:
                    from common.audit import ACTION_BRASSIN_CREATED, log_event
                    log_event(
                        tenant_id=app.storage.user.get("tenant_id"),
                        user_email=app.storage.user.get("email"),
                        action=ACTION_BRASSIN_CREATED,
                        details={"brassin_ids": created_ids, "gouts": _gouts_eb},
                    )
                except Exception:
                    _log.debug("Audit log_event brassin_created failed", exc_info=True)
                ui.notify(
                    f"{len(created_ids)} brassin(s) créé(s) !",
                    type="positive", icon="check",
                )
            for err in errors:
                ui.notify(err, type="negative")

        # Dialogue de confirmation avec preview détaillée (action irréversible dans l'ERP)
        _detail_lines = [
            f"{g} — {_vol_par_gout.get(g, 0):.0f} L — {_prod_labels[_product_indices[g]] if _product_indices.get(g) is not None and _prod_labels else '—'}"
            for g in _gouts_eb
        ]
        _confirm_dlg, _confirm_msg, _confirm_action = confirm_dialog(
            title="Confirmer la création des brassins ?",
            message=f"{len(_gouts_eb)} brassin(s) seront créés dans EasyBeer.",
            action_label="Créer",
            action_icon="rocket_launch",
            danger=True,
        )
        # Enrichir le dialog avec la preview détaillée
        with _confirm_dlg:
            with ui.column().classes("gap-1 q-mt-xs q-mb-sm"):
                for line in _detail_lines:
                    ui.label(f"• {line}").classes("text-body2").style("font-weight: 500")
                ui.label("Cette action est irréversible.").classes("text-caption text-grey-6 q-mt-xs")

        async def _confirmed_create():
            _confirm_dlg.close()
            _create_btn.disable()
            _create_spinner.set_visibility(True)
            try:
                await do_create_brassins()
            finally:
                _create_btn.enable()
                _create_spinner.set_visibility(False)

        _confirm_action.on_click(_confirmed_create)

        with ui.row().classes("w-full items-center gap-3 q-mt-md"):
            _create_btn = ui.button(
                "Créer les brassins",
                icon="rocket_launch",
                on_click=_confirm_dlg.open,
            ).classes("flex-1").props("color=green-8 unelevated")
            _create_spinner = ui.spinner("dots", size="md", color="green-8")
            _create_spinner.set_visibility(False)
