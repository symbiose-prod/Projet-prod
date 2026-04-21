"""
common/services/production_service.py
=====================================
Service domaine : calculs de production (planification, optimiseur, répartition
goûts, split cuves 7200L/5200L).

Ne dépend ni de NiceGUI ni de ``pages/`` — thread-safe, bloquant (l'appelant
doit passer par ``asyncio.to_thread`` pour ne pas bloquer l'event loop).

Expose aussi les helpers ``_fetch_eb_products`` / ``invalidate_eb_products_cache``
utilisés par la section "Création brassin EasyBeer" (``pages/_production_easybeer``).
"""
from __future__ import annotations

import logging

import pandas as pd

from core.optimizer import compute_plan

_log = logging.getLogger("ferment.production")

# ─── Produits EasyBeer (cache centralisé dans products.py) ───────────────────


def invalidate_eb_products_cache() -> None:
    """Invalide le cache produits (appelé manuellement via bouton « Rafraîchir »)."""
    from common.easybeer.products import invalidate_products_cache
    invalidate_products_cache()


def _fetch_eb_products() -> list[dict]:
    """Produits EasyBeer — délègue au cache centralisé dans products.py (TTL 1h)."""
    from common.easybeer import get_all_products
    return get_all_products()


_SECONDARY_BRANDS = {"igeba", "niko", "inter", "water"}


def _parse_iso_to_dmy(raw: str | int | float | None) -> str:
    """Parse une date ISO, epoch-ms ou string en format DD/MM/YYYY.

    Gère les chaînes courtes, les timestamps epoch en millisecondes,
    et retourne une chaîne vide si le format est invalide.
    """
    if isinstance(raw, str) and len(raw) >= 10:
        try:
            return f"{raw[8:10]}/{raw[5:7]}/{raw[:4]}"
        except (IndexError, ValueError):
            return ""
    if isinstance(raw, (int, float)) and raw > 0:
        try:
            import datetime as _dt
            return _dt.datetime.fromtimestamp(
                raw / 1000, tz=_dt.UTC
            ).strftime("%d/%m/%Y")
        except (OSError, ValueError, OverflowError):
            return ""
    return ""


def _auto_match(gout: str, prod_labels: list[str]) -> int:
    """Retourne l'index du produit EasyBeer dont le libellé correspond le mieux au goût.

    Stratégie en 3 passes :
    1. Correspondance substring exacte (ex: "Original" in "Kéfir Original - 0.0°")
    2. Tous les mots du goût présents dans le label (ex: "Infusion Mélisse" →
       "Infusion" ∈ label AND "Mélisse" ∈ label, même si séparés par d'autres mots)
    3. Score de mots communs (best effort)

    Privilégie les produits non-secondaires (Igeba, Niko, Inter, Water).
    """
    import unicodedata

    def _normalize(s: str) -> str:
        """Supprime accents et met en minuscule."""
        nfkd = unicodedata.normalize("NFKD", s.lower())
        return "".join(c for c in nfkd if not unicodedata.combining(c))

    g_norm = _normalize(gout)
    g_words = set(g_norm.split())

    # Passe 1 : correspondance substring exacte
    candidates_exact: list[int] = []
    for i, lbl in enumerate(prod_labels):
        if g_norm in _normalize(lbl):
            candidates_exact.append(i)

    if candidates_exact:
        return _pick_best(candidates_exact, prod_labels)

    # Passe 2 : tous les mots du goût présents dans le label
    candidates_words: list[int] = []
    for i, lbl in enumerate(prod_labels):
        lbl_norm = _normalize(lbl)
        if all(w in lbl_norm for w in g_words):
            candidates_words.append(i)

    if candidates_words:
        return _pick_best(candidates_words, prod_labels)

    # Passe 3 : score de mots communs (au moins 1 mot en commun)
    best_score = 0
    best_idx = 0
    for i, lbl in enumerate(prod_labels):
        lbl_words = set(_normalize(lbl).split())
        score = len(g_words & lbl_words)
        # Bonus pour non-marque secondaire
        lbl_low = lbl.lower()
        if any(brand in lbl_low for brand in _SECONDARY_BRANDS):
            score -= 0.5
        if score > best_score:
            best_score = score
            best_idx = i

    if best_score > 0:
        _log.info("_auto_match: goût '%s' → best-effort match '%s' (score=%.1f)",
                   gout, prod_labels[best_idx], best_score)
    else:
        _log.warning("_auto_match: goût '%s' → aucun match trouvé, fallback index 0 '%s'",
                      gout, prod_labels[0] if prod_labels else "?")
    return best_idx


def _pick_best(candidates: list[int], prod_labels: list[str]) -> int:
    """Parmi les candidats, préférer celui qui n'est pas une marque secondaire."""
    if len(candidates) == 1:
        return candidates[0]
    for i in candidates:
        lbl_low = prod_labels[i].lower()
        if not any(brand in lbl_low for brand in _SECONDARY_BRANDS):
            return i
    return candidates[0]


# ─── Productions en cours (brassins EasyBeer) ───────────────────────────────


def _match_brassin_to_gout(produit_libelle: str, gouts_connus: list[str]) -> str | None:
    """Retourne le GoutCanon qui matche le libellé produit du brassin.

    Teste les goûts les plus longs d'abord pour éviter les faux positifs
    (ex: "Citron Gingembre" doit matcher "Citron Gingembre" et pas "Citron").
    """
    lbl = produit_libelle.lower()
    for g in sorted(gouts_connus, key=len, reverse=True):
        if g.lower() in lbl:
            return g
    return None


def _fetch_ongoing_productions(df: pd.DataFrame) -> dict:
    """Récupère les brassins en cours et les agrège par GoutCanon.

    Retourne {"par_gout": {GoutCanon: vol_hL}, "detail": [...], "total_hl": float}.
    """
    from common.easybeer import get_brassins_en_cours_cached

    brassins = get_brassins_en_cours_cached()
    if not brassins:
        return {"par_gout": {}, "detail": [], "total_hl": 0.0}

    gouts_connus = df["GoutCanon"].dropna().unique().tolist()
    par_gout: dict[str, float] = {}
    detail: list[dict] = []

    for b in brassins:
        # Filtrer annulés / terminés
        if b.get("annule") or b.get("termine"):
            continue

        produit = b.get("produit") or {}
        libelle = produit.get("libelle", "")
        volume_l = float(b.get("volume") or 0)
        if volume_l < 100:  # ignorer les petits brassins (tests)
            continue

        gout = _match_brassin_to_gout(libelle, gouts_connus)

        # État
        etat_obj = b.get("etat") or {}
        etat_libelle = etat_obj.get("libelle", "En cours")

        # Date conditionnement prévue
        date_cond = _parse_iso_to_dmy(b.get("dateConditionnementPrevue"))

        vol_hl = round(volume_l / 100.0, 2)

        detail.append({
            "nom": b.get("nom", ""),
            "produit": libelle,
            "gout": gout or "—",
            "volume_l": int(volume_l),
            "volume_hl": vol_hl,
            "etat": etat_libelle,
            "date_conditionnement": date_cond,
        })

        if gout:
            par_gout[gout] = par_gout.get(gout, 0.0) + vol_hl

    total_hl = round(sum(par_gout.values()), 2)
    return {"par_gout": par_gout, "detail": detail, "total_hl": total_hl}


def _fetch_planned_productions(df: pd.DataFrame | None = None) -> dict:
    """Récupère les brassins planifiés (non encore démarrés).

    Retourne {"par_gout": {GoutCanon: vol_hL}, "detail": [...], "total_hl": float}.
    """
    from common.easybeer.brassins import get_brassin_detail, get_brassins_planifies

    raw_list = get_brassins_planifies(30)
    if not raw_list:
        return {"par_gout": {}, "detail": [], "total_hl": 0.0}

    gouts_connus = (
        df["GoutCanon"].dropna().unique().tolist() if df is not None else []
    )

    par_gout: dict[str, float] = {}
    detail: list[dict] = []
    total_hl = 0.0

    for raw in raw_list:
        bid = raw.get("idBrassin")
        if not bid:
            continue
        try:
            b = get_brassin_detail(bid)
        except Exception:
            b = raw

        etat_obj = b.get("etat") or {}
        etat_code = etat_obj.get("code", "")
        if etat_code != "PLANIFIE":
            continue

        produit = b.get("produit") or {}
        libelle = produit.get("libelle", "")
        volume_l = float(b.get("volume") or 0)
        if volume_l < 100:
            continue

        gout = _match_brassin_to_gout(libelle, gouts_connus) if gouts_connus else ""

        # Date début
        date_debut = _parse_iso_to_dmy(
            b.get("dateDebutPlanificationFormulaire")
            or b.get("dateDebutCalendrier")
            or b.get("dateDebutFormulaire")
        )

        # Date conditionnement
        date_cond = _parse_iso_to_dmy(b.get("dateConditionnementPrevue"))

        vol_hl = round(volume_l / 100.0, 2)
        total_hl += vol_hl

        if gout:
            par_gout[gout] = par_gout.get(gout, 0.0) + vol_hl

        detail.append({
            "nom": b.get("nom", ""),
            "produit": libelle,
            "gout": gout or "—",
            "volume_l": int(volume_l),
            "volume_hl": vol_hl,
            "etat": "Planifié",
            "date_debut": date_debut or "—",
            "date_conditionnement": date_cond or "—",
        })

    return {"par_gout": par_gout, "detail": detail, "total_hl": round(total_hl, 2)}


def _inject_ongoing_volumes(
    df: pd.DataFrame, par_gout: dict[str, float],
) -> pd.DataFrame:
    """Ajoute les volumes en cours au stock disponible, au prorata des ventes par format.

    Ceci augmente l'autonomie dans l'optimiseur → réduit la production proposée
    pour les goûts qui ont déjà des brassins en cours.
    """
    df = df.copy()
    for gout, vol_hl in par_gout.items():
        mask = df["GoutCanon"] == gout
        if not mask.any():
            continue
        ventes = df.loc[mask, "Volume vendu (hl)"]
        total_ventes = ventes.sum()
        if total_ventes > 0:
            df.loc[mask, "Volume disponible (hl)"] += vol_hl * (ventes / total_ventes)
        else:
            n = mask.sum()
            df.loc[mask, "Volume disponible (hl)"] += vol_hl / n
    return df


# ─── Construction tableau final ──────────────────────────────────────────────

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


# ─── Vérification disponibilité matières premières ──────────────────────────

def _check_mp_availability(
    gouts_cibles: list[str],
    volume_details: dict,
    volume_cible: float,
    mode_prod: str,
    *,
    TANK_CONFIGS: dict,
    DEFAULT_LOSS_LARGE: int,
    DEFAULT_LOSS_SMALL: int,
    all_mps_prefetched: list[dict] | None = None,
    product_ids_by_gout: dict[str, int] | None = None,
) -> dict:
    """Vérifie si les MP (ingrédients recette) suffisent pour la production.

    Retourne {"status": "ok"|"warning"|"error", "items": [...], "error_msg": ""}.
    Exécutée dans le thread pool — pas d'UI.
    """
    from common.brassin_builder import scale_recipe_ingredients
    from common.easybeer import (
        get_all_matieres_premieres,
        get_product_detail,
        is_configured,
    )

    if not is_configured():
        return {"status": "error", "items": [], "error_msg": "EasyBeer non configuré"}

    nb_gouts = len(gouts_cibles)
    if nb_gouts == 0:
        return {"status": "ok", "items": [], "error_msg": ""}

    # 1. Volume par goût pour mise à l'échelle recette.
    #    En mode split, V_dilution inclut la part de perte de transfert
    #    pour que le total des ingrédients de base = volume fermenté.
    vol_par_gout: dict[str, float] = {}
    if volume_details:
        for g in gouts_cibles:
            if g in volume_details:
                vol_par_gout[g] = volume_details[g].get("V_dilution", volume_details[g]["V_start"])
            else:
                _tank = TANK_CONFIGS.get(mode_prod) or TANK_CONFIGS["Cuve de 7200L (1 goût)"]
                vol_par_gout[g] = float(_tank["capacity"])
    else:
        perte = DEFAULT_LOSS_LARGE if volume_cible > 50 else DEFAULT_LOSS_SMALL
        for g in gouts_cibles:
            vol_par_gout[g] = (volume_cible / nb_gouts) * 100 + perte

    _log.info(
        "MP check: gouts=%s, volume_cible=%.1f hL, mode=%s, vol_par_gout=%s",
        gouts_cibles, volume_cible, mode_prod,
        {g: f"{v:.0f}L" for g, v in vol_par_gout.items()},
    )

    # 2. Matcher goûts → produits EasyBeer
    eb_products = _fetch_eb_products()
    if not eb_products:
        return {"status": "error", "items": [], "error_msg": "Aucun produit EasyBeer"}

    prod_labels = [p.get("libelle", "") for p in eb_products]

    # 3. Pour chaque goût : recette → ingrédients → agréger besoins
    total_needs: dict[int, dict] = {}  # idMatierePremiere → {libelle, qty, unite}
    needs_by_gout: dict[str, dict[int, dict]] = {}  # goût → {idMP → {libelle, qty, unite}}

    for g in gouts_cibles:
        vol_l = vol_par_gout.get(g, 0)
        if vol_l <= 0:
            continue

        needs_by_gout[g] = {}
        # Réutiliser l'id_produit résolu en passe 2 si disponible
        id_produit = (product_ids_by_gout or {}).get(g)
        if id_produit is None:
            idx = _auto_match(g, prod_labels)
            id_produit = eb_products[idx]["idProduit"]
        _log.info(
            "MP check: goût '%s' → produit EasyBeer id=%d (vol_l=%.0f L)",
            g, id_produit, vol_l,
        )

        try:
            detail = get_product_detail(id_produit)
            recettes = detail.get("recettes") or []
            if not recettes:
                continue

            scaled = scale_recipe_ingredients(recettes[0], vol_l)
            for ing in scaled:
                mp = ing.get("matierePremiere") or {}
                id_mp = mp.get("idMatierePremiere")
                if id_mp is None:
                    continue

                mp_type = (mp.get("type") or {}).get("code", "")
                qty = float(ing.get("quantite", 0) or 0)
                unite = (ing.get("unite") or {}).get("symbole", "")
                libelle = mp.get("libelle", f"MP #{id_mp}")

                # Ignorer les emballages (gérés via stock, pas recette)
                if mp_type.startswith("CONDITIONNEMENT"):
                    continue

                if id_mp in total_needs:
                    total_needs[id_mp]["qty"] += qty
                else:
                    total_needs[id_mp] = {"libelle": libelle, "qty": qty, "unite": unite}

                # Per-flavor tracking
                if id_mp in needs_by_gout[g]:
                    needs_by_gout[g][id_mp]["qty"] += qty
                else:
                    needs_by_gout[g][id_mp] = {"libelle": libelle, "qty": qty, "unite": unite}
        except Exception as exc:
            _log.warning("MP check: erreur recette goût %s: %s", g, exc, exc_info=True)
            continue

    if not total_needs:
        return {
            "status": "ok", "items": [], "items_by_gout": {},
            "error_msg": "",
        }

    # 4. Stock actuel (réutilise le prefetch si disponible)
    try:
        all_mps = all_mps_prefetched if all_mps_prefetched is not None else get_all_matieres_premieres()
    except Exception as exc:
        return {"status": "error", "items": [], "error_msg": f"Erreur stocks MP: {exc}"}

    stock_by_id: dict[int, float] = {
        m["idMatierePremiere"]: float(m.get("quantiteVirtuelle", 0) or 0)
        for m in all_mps
        if m.get("idMatierePremiere") is not None
    }

    # 5. Comparer besoins vs stock
    items = []
    has_shortage = False
    for id_mp, need in sorted(total_needs.items(), key=lambda x: x[1]["libelle"]):
        stock = stock_by_id.get(id_mp, 0.0)
        besoin = need["qty"]
        ecart = stock - besoin
        ok = ecart >= 0
        if not ok:
            has_shortage = True
        items.append({
            "id_mp": id_mp,
            "libelle": need["libelle"],
            "besoin": round(besoin, 2),
            "stock": round(stock, 2),
            "ecart": round(ecart, 2),
            "unite": need["unite"],
            "ok": ok,
        })

    # Construire items par goût (même structure, avec stock individuel)
    _items_by_gout: dict[str, list] = {}
    for g, g_needs in needs_by_gout.items():
        g_items = []
        for id_mp, need in sorted(g_needs.items(), key=lambda x: x[1]["libelle"]):
            stock = stock_by_id.get(id_mp, 0.0)
            besoin = need["qty"]
            ecart = stock - besoin
            g_items.append({
                "id_mp": id_mp,
                "libelle": need["libelle"],
                "besoin": round(besoin, 2),
                "stock": round(stock, 2),
                "ecart": round(ecart, 2),
                "unite": need["unite"],
                "ok": ecart >= 0,
            })
        _items_by_gout[g] = g_items

    return {
        "status": "warning" if has_shortage else "ok",
        "items": items,
        "items_by_gout": _items_by_gout,
        "error_msg": "",
        "_all_mps": all_mps,
    }


def _check_emballages(df_final: pd.DataFrame, *, all_mps_prefetched: list[dict] | None = None) -> dict:
    """Calcule les besoins en emballages depuis le plan de production.

    Pour chaque produit fini dans df_final, récupère les éléments de
    conditionnement (capsules, étiquettes, cartons) via l'API stock produit
    EasyBeer, puis compare au stock actuel.
    Ajoute aussi les bouteilles (CONTENANT) déduits du nombre de bouteilles.

    Retourne {"emb_status": "ok"|"warning"|"error", "emballages": [...]}.
    Chaque item : {id_mp, libelle, besoin, stock, ecart, unite, ok}.
    """
    from common.easybeer import (
        get_all_matieres_premieres,
        is_configured,
    )

    if not is_configured():
        return {"emb_status": "error", "emballages": [], "emb_error": "EasyBeer non configuré"}

    if df_final.empty:
        return {"emb_status": "ok", "emballages": []}

    # 1. Matcher goûts → produits EasyBeer
    eb_products = _fetch_eb_products()
    if not eb_products:
        return {"emb_status": "error", "emballages": [], "emb_error": "Aucun produit EasyBeer"}
    prod_labels = [p.get("libelle", "") for p in eb_products]

    # 3. Pour chaque ligne du plan : agréger besoins emballages
    #    OPTIMISATION : utilise la table BOM locale (product_bom) au lieu de
    #    N appels API get_stock_produit_detail() — passe de ~50 appels à 1 requête DB.
    emb_needs: dict[int, dict] = {}  # idMatierePremiere → {libelle, qty, unite}

    try:
        all_mps = all_mps_prefetched if all_mps_prefetched is not None else get_all_matieres_premieres()
    except Exception as exc:
        _log.warning("Erreur get_all_matieres_premieres pour emballages: %s", exc)
        all_mps = []

    # Charger le BOM validé depuis la DB (1 requête SQL, pas d'appel API)
    from common.product_bom import get_all_bom
    bom_entries = get_all_bom()
    # Index: (id_produit, format_code) → [{id_mp, mp_label, qty_per_unit}]
    bom_by_key: dict[tuple[int, str], list[dict]] = {}
    for e in bom_entries:
        key = (e["id_produit"], e["format_code"])
        bom_by_key.setdefault(key, []).append(e)

    # Index des unités + types MP pour enrichir les résultats
    mp_unite_by_id: dict[int, str] = {}
    mp_type_by_id: dict[int, str] = {}
    for mp in all_mps:
        mp_id = mp.get("idMatierePremiere")
        if mp_id is not None:
            mp_unite_by_id[mp_id] = (mp.get("unite") or {}).get("symbole", "")
            mp_type_by_id[mp_id] = (mp.get("type") or {}).get("code", "")

    active_rows = df_final[df_final["Cartons à produire (arrondi)"] > 0]
    _bom_hits, _bom_misses = 0, 0
    for _, row in active_rows.iterrows():
        produit_name = str(row["Produit"])
        cartons = int(row["Cartons à produire (arrondi)"])
        btl_per_carton = int(row["Bouteilles/carton"])
        vol_carton_hl = float(row["Volume/carton (hL)"])

        # Volume bouteille en cL pour le fmt_str
        vol_btl_l = vol_carton_hl / btl_per_carton * 100 if btl_per_carton > 0 else 0
        vol_cl = int(round(vol_btl_l * 100))
        fmt_str = f"{btl_per_carton}x{vol_cl}"

        # Trouver le idProduit en matchant le nom Produit
        _pn_low = produit_name.lower()
        _best_idx, _best_len = 0, 0
        for _i, _lbl in enumerate(prod_labels):
            _ll = _lbl.lower()
            if _ll in _pn_low and len(_ll) > _best_len:
                _best_idx, _best_len = _i, len(_ll)
            elif _pn_low in _ll and len(_pn_low) > _best_len:
                _best_idx, _best_len = _i, len(_pn_low)
        id_produit = eb_products[_best_idx]["idProduit"]

        # Lire le BOM depuis la DB (pas d'appel API !)
        bom_components = bom_by_key.get((id_produit, fmt_str), [])
        if not bom_components:
            _bom_misses += 1
            _log.debug(
                "Emballages: pas de BOM pour (%s, %s) produit=%s",
                id_produit, fmt_str, produit_name,
            )
            continue

        _bom_hits += 1
        for comp in bom_components:
            id_mp = comp["id_mp"]
            # Ignorer les ingrédients (gérés dans la section MP, pas emballages)
            if mp_type_by_id.get(id_mp, "").startswith("INGREDIENT"):
                continue
            qty_per_unit = float(comp.get("qty_per_unit", 0))
            besoin = qty_per_unit * cartons
            libelle = comp.get("mp_label", f"MP #{id_mp}")
            unite = mp_unite_by_id.get(id_mp, "")

            if id_mp in emb_needs:
                emb_needs[id_mp]["qty"] += besoin
            else:
                emb_needs[id_mp] = {"libelle": libelle, "qty": besoin, "unite": unite}

    _log.info("Emballages BOM: %d hits, %d misses", _bom_hits, _bom_misses)

    if not emb_needs:
        return {"emb_status": "ok", "emballages": []}

    # 5. Comparer au stock (MP + bouteilles/contenants)
    stock_by_id: dict[int, float] = {
        m["idMatierePremiere"]: float(m.get("quantiteVirtuelle", 0) or 0)
        for m in all_mps
        if m.get("idMatierePremiere") is not None
    }
    # Enrichir avec le stock des bouteilles (contenants séparés dans EasyBeer)
    try:
        from common.easybeer.stocks import get_bottle_stock
        bottle_stock = get_bottle_stock()
        for cont_id, qty in bottle_stock.items():
            # Les clés peuvent être des strings (JSON) ou des ints (API)
            int_id = int(cont_id)
            if int_id not in stock_by_id:
                stock_by_id[int_id] = float(qty)
    except Exception:
        _log.debug("Erreur chargement stock bouteilles", exc_info=True)

    emb_items: list[dict] = []
    has_shortage = False
    for id_mp, need in sorted(emb_needs.items(), key=lambda x: x[1]["libelle"]):
        stock = stock_by_id.get(id_mp, 0.0)
        besoin = need["qty"]
        ecart = stock - besoin
        ok = ecart >= 0
        if not ok:
            has_shortage = True
        emb_items.append({
            "id_mp": id_mp,
            "libelle": need["libelle"],
            "besoin": round(besoin, 1),
            "stock": round(stock, 1),
            "ecart": round(ecart, 1),
            "unite": need["unite"],
            "ok": ok,
        })

    return {
        "emb_status": "warning" if has_shortage else "ok",
        "emballages": emb_items,
    }


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
    *,
    TANK_CONFIGS: dict,
    DEFAULT_LOSS_LARGE: int,
    DEFAULT_LOSS_SMALL: int,
    split_volumes: list[float] | None = None,
    split_flavor_order: list[str] | None = None,
    include_planned: bool = False,
) -> dict:
    """Passe 0 (en cours) + Passe 1 (optimiseur) + Passe 2 (EasyBeer) — aucun appel UI."""
    # ── PASSE 0 : Productions en cours + planifiées (fetch en parallèle) ──
    ongoing: dict = {"par_gout": {}, "detail": [], "total_hl": 0.0}
    planned: dict = {"par_gout": {}, "detail": [], "total_hl": 0.0}
    try:
        from common.easybeer import is_configured as _eb_conf_p0
        if _eb_conf_p0():
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=2) as _pool_p0:
                _f_ongoing = _pool_p0.submit(_fetch_ongoing_productions, df_in_filtered)
                _f_planned = _pool_p0.submit(_fetch_planned_productions, df_in_filtered)

            try:
                ongoing = _f_ongoing.result()
                if ongoing["par_gout"]:
                    df_in_filtered = _inject_ongoing_volumes(df_in_filtered, ongoing["par_gout"])
                    _log.info(
                        "Productions en cours intégrées : %s (total %.1f hL)",
                        ongoing["par_gout"], ongoing["total_hl"],
                    )
            except Exception as exc:
                _log.warning("Erreur fetch brassins en cours: %s", exc, exc_info=True)

            try:
                planned = _f_planned.result()
                if planned["detail"]:
                    _log.info(
                        "Productions planifiées : %d brassins (total %.1f hL)",
                        len(planned["detail"]), planned["total_hl"],
                    )
                if include_planned and planned["par_gout"]:
                    df_in_filtered = _inject_ongoing_volumes(
                        df_in_filtered, planned["par_gout"],
                    )
                    _log.info(
                        "Volumes planifiés intégrés au stock : %s",
                        planned["par_gout"],
                    )
            except Exception as exc:
                _log.warning("Erreur fetch brassins planifiés: %s", exc, exc_info=True)
    except Exception as exc:
        _log.warning("Erreur fetch productions: %s", exc, exc_info=True)

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

    # Réordonnancement des goûts (Split 7200L — assignation utilisateur)
    if split_flavor_order and set(split_flavor_order) == set(gouts_cibles):
        gouts_cibles = list(split_flavor_order)

    # ── PASSE 2 : Aromatisation (tous les goûts)
    volume_details: dict = {}
    if gouts_cibles:
        from common.easybeer import (
            compute_aromatisation_volume,
            compute_dilution_ingredients,
            compute_v_start_max,
        )
        from common.easybeer import (
            is_configured as _eb_conf_p2,
        )
        _tank_cfg = TANK_CONFIGS[mode_prod]
        _C = _tank_cfg["capacity"]
        _Lt = _tank_cfg["transfer_loss"]
        _Lb = _tank_cfg["bottling_loss"]

        # Split 7200L avec 2+ goûts : fermentation 7200L → 2×5200L garde
        _split_cfg = _tank_cfg.get("split")
        _is_split_2 = bool(_split_cfg and effective_nb_gouts >= 2)
        if _is_split_2:
            _C_garde = _split_cfg["garde_capacity"]
            _Lb_split = _split_cfg["bottling_loss_per_flavor"]
            _V_total_dispo = _C - _Lt  # 6800 L
            if split_volumes and len(split_volumes) == effective_nb_gouts:
                _split_vol_list = list(split_volumes)
            else:
                _split_vol_list = [_V_total_dispo / effective_nb_gouts] * effective_nb_gouts
        else:
            _C_garde = _C
            _Lb_split = _Lb
            _split_vol_list = []  # pas utilisé

        _eb_prods_p2: list = []
        _labels_p2: list = []
        _eb_available = _eb_conf_p2()
        if _eb_available:
            try:
                _eb_prods_p2 = _fetch_eb_products()
                _labels_p2 = [p.get("libelle", "") for p in _eb_prods_p2]
            except Exception:
                _log.warning("Erreur chargement produits EasyBeer (passe 2)", exc_info=True)
                _eb_available = False

        for _gout_idx_p2, _gout_p2 in enumerate(gouts_cibles):
            _A_R, _R = 0.0, 0.0
            _id_prod_p2 = None
            _matched_idx = 0

            if _eb_available and _eb_prods_p2:
                try:
                    _matched_idx = _auto_match(_gout_p2, _labels_p2)
                    _id_prod_p2 = _eb_prods_p2[_matched_idx]["idProduit"]
                    _A_R, _R = compute_aromatisation_volume(_id_prod_p2)
                except (ValueError, TypeError, KeyError, IndexError) as exc:
                    _log.warning("Erreur calcul aromatisation pour %s: %s", _gout_p2, exc, exc_info=True)
                    _A_R, _R = 0.0, 0.0

            # Calcul V_start et V_bottled
            if _is_split_2:
                # Volume alloué à ce goût (répartition personnalisable)
                _V_start_base = (
                    _split_vol_list[_gout_idx_p2]
                    if _gout_idx_p2 < len(_split_vol_list)
                    else _split_vol_list[-1]
                )
                # Cappé par la capacité de la cuve de garde
                _V_start_max, _ = compute_v_start_max(_C_garde, 0, _Lb_split, _A_R, _R)
                _V_start = min(_V_start_base, _V_start_max)
                _V_aroma = _A_R * (_V_start / _R) if _R > 0 else 0.0
                _V_bottled = max(_V_start + _V_aroma - _Lb_split, 0.0)
            else:
                _V_start, _V_bottled = compute_v_start_max(_C, _Lt, _Lb, _A_R, _R)
                _V_aroma = _A_R * (_V_start / _R) if _R > 0 else 0.0

            # En mode split, les ingrédients de base (sirop, fermentation)
            # doivent être proportionnés au volume FERMENTÉ (7200 L),
            # pas au volume en cuve de garde (3400 L).
            # Chaque goût porte sa part de la perte de transfert.
            _V_for_dilution = (
                _V_start * _C / (_C - _Lt)
                if _is_split_2 and (_C - _Lt) > 0
                else _V_start
            )

            _is_infusion_p2 = False
            _dilution_p2: dict = {}
            if _id_prod_p2 is not None:
                try:
                    _prod_label_p2 = _eb_prods_p2[_matched_idx].get("libelle", "")
                    _is_infusion_p2 = (
                        "infusion" in _prod_label_p2.lower()
                        or _prod_label_p2.upper().startswith("EP")
                    )
                except (IndexError, KeyError, AttributeError):
                    _log.debug("Erreur détection infusion pour %s", _gout_p2, exc_info=True)
                try:
                    _dilution_p2 = compute_dilution_ingredients(_id_prod_p2, _V_for_dilution)
                except (ValueError, TypeError, KeyError) as exc:
                    _log.warning("Erreur calcul dilution p2 pour %s: %s", _gout_p2, exc, exc_info=True)
                    _dilution_p2 = {}

            volume_details[_gout_p2] = {
                "V_start": _V_start,
                "V_dilution": _V_for_dilution,
                "A_R": _A_R,
                "R": _R,
                "V_aroma": _V_aroma,
                "V_bottled": _V_bottled,
                "capacity": _C_garde if _is_split_2 else _C,
                "transfer_loss": 0 if _is_split_2 else _Lt,
                "bottling_loss": _Lb_split if _is_split_2 else _Lb,
                "is_infusion": _is_infusion_p2,
                "dilution_ingredients": _dilution_p2,
                "id_produit": _id_prod_p2,
            }

        # Relance si le volume total a changé
        _total_bottled = sum(vd["V_bottled"] for vd in volume_details.values())
        _volume_cible_recalc = _total_bottled / 100.0
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
            except (ValueError, KeyError, pd.errors.MergeError) as exc:
                _log.exception("Erreur compute_plan: %s", exc)

    # ── Ajuster df_calc pour le mode Split ──────────────────────────
    # L'optimiseur distribue le volume total au prorata des ventes.
    # En mode split, le volume par goût est fixé par le slider :
    # on rescale X_adj pour que chaque goût = V_bottled du split.
    _tank_adj = TANK_CONFIGS.get(mode_prod) or {}
    if (
        _tank_adj.get("split")
        and volume_details
        and len(volume_details) >= 2
        and not df_calc.empty
    ):
        for _g_adj in gouts_cibles:
            if _g_adj in volume_details:
                _v_target_hl = volume_details[_g_adj]["V_bottled"] / 100.0
                _mask_adj = df_calc["GoutCanon"] == _g_adj
                _current_hl = df_calc.loc[_mask_adj, "X_adj (hL)"].sum()
                if _current_hl > 1e-9:
                    df_calc.loc[_mask_adj, "X_adj (hL)"] *= _v_target_hl / _current_hl

    df_final = _build_final_table(df_all, df_calc, gouts_cibles, overrides)

    # ── PASSE 3 : Vérification disponibilité MP ──────────────────
    mp_check: dict = {"status": "error", "items": [], "error_msg": ""}
    try:
        # Construire le mapping goût → id_produit depuis la passe 2
        _product_ids_p3 = {
            g: vd["id_produit"]
            for g, vd in volume_details.items()
            if vd.get("id_produit") is not None
        }
        mp_check = _check_mp_availability(
            gouts_cibles=gouts_cibles,
            volume_details=volume_details,
            volume_cible=volume_cible,
            mode_prod=mode_prod,
            TANK_CONFIGS=TANK_CONFIGS,
            DEFAULT_LOSS_LARGE=DEFAULT_LOSS_LARGE,
            DEFAULT_LOSS_SMALL=DEFAULT_LOSS_SMALL,
            product_ids_by_gout=_product_ids_p3,
        )
    except Exception as exc:
        _log.warning("Erreur vérification MP: %s", exc, exc_info=True)
        mp_check = {"status": "error", "items": [], "error_msg": str(exc)}

    # ── PASSE 4 : Stock emballages (depuis stock EasyBeer, pas recettes) ──
    #    Réutilise les MPs déjà chargées en passe 3 pour éviter un appel API redondant.
    _prefetched_mps = mp_check.get("_all_mps")
    emb_check: dict = {"emb_status": "ok", "emballages": []}
    try:
        emb_check = _check_emballages(df_final, all_mps_prefetched=_prefetched_mps)
    except Exception as exc:
        _log.warning("Erreur vérification emballages: %s", exc, exc_info=True)
        emb_check = {"emb_status": "error", "emballages": [], "emb_error": str(exc)}

    # Nettoyer le champ interne _all_mps (ne doit pas remonter à l'UI)
    mp_check.pop("_all_mps", None)

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
        "ongoing": ongoing,
        "planned": planned,
        "mp_check": mp_check,
        "emb_check": emb_check,
    }
