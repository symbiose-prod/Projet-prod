"""
common/bom_detection.py
=======================
Auto-detection of BOM entries from EasyBeer data.

Fetches the finished-product stock list (POST /stock/produits), then for
each product-format calls GET /stock/produit/edition/{id} to read the
conditioning elements (étiquettes, capsules, cartons) configured in EasyBeer.

Detected entries are stored with ``validated=False`` so the user can
review and confirm them on the /nomenclatures page.
"""
from __future__ import annotations

import logging
import os
from typing import Any

_log = logging.getLogger("ferment.bom_detection")


# ─── Fetch stock produits (finished products with formats) ────────────────

def _fetch_stock_produits() -> dict[str, Any]:
    """POST /stock/produits → all finished-product stock consolidations."""
    from common.easybeer._client import BASE, _auth, _check_response, _safe_json, get_session

    id_brasserie = int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013"))
    r = get_session().post(
        f"{BASE}/stock/produits",
        json={"idBrasserie": id_brasserie},
        auth=_auth(),
        timeout=30,
    )
    _check_response(r, "stock/produits")
    return _safe_json(r, "stock/produits")


def _build_stock_map(
    produits_data: dict[str, Any],
) -> dict[tuple[int, str], dict[str, Any]]:
    """Build (idProduit, format_code) → {sid, libelle, contenance, lot_qty}.

    Parses the consolidation tree from POST /stock/produits.
    """
    from common.easybeer.products import get_all_products

    # idProduit → libelle lookup
    id_to_label: dict[int, str] = {}
    for p in get_all_products():
        pid = p.get("idProduit")
        lib = (p.get("libelle") or "").strip()
        if pid and lib:
            id_to_label[pid] = lib

    stock_map: dict[tuple[int, str], dict[str, Any]] = {}
    for prod in produits_data.get("consolidationsFilles", []):
        for conso in prod.get("consolidationsFilles", []):
            sid = conso.get("id")
            if not sid:
                continue
            produit = conso.get("produit") or {}
            id_produit = produit.get("idProduit")
            cont = conso.get("contenant") or {}
            contenance = float(cont.get("contenance", 0) or 0)
            lot = conso.get("lot") or {}
            lot_qty = int(lot.get("quantite", 0) or 0)
            if id_produit and contenance and lot_qty:
                fmt_str = f"{lot_qty}x{int(contenance * 100)}"
                stock_map[(id_produit, fmt_str)] = {
                    "sid": sid,
                    "libelle": id_to_label.get(id_produit, f"Produit #{id_produit}"),
                    "contenance": contenance,
                    "lot_qty": lot_qty,
                }

    return stock_map


# ─── Read conditioning elements from EasyBeer ─────────────────────────────

def _detect_from_stock_detail(
    id_produit: int,
    product_label: str,
    format_code: str,
    id_stock_produit: int,
    lot_qty: int = 0,
) -> list[dict[str, Any]] | None:
    """Fetch GET /stock/produit/edition/{id} and extract conditioning elements + bouteille.

    Returns BOM entry dicts ready for ``bulk_upsert_bom()``,
    or ``None`` if rate-limited (caller should stop).
    """
    from common.easybeer.stocks import get_stock_produit_detail

    try:
        detail = get_stock_produit_detail(id_stock_produit)
    except Exception as exc:
        _log.warning(
            "Cannot fetch stock detail %d for %s %s: %s",
            id_stock_produit, product_label, format_code, exc,
        )
        # Rate-limit → signal caller to stop
        if "rate-limit" in str(exc).lower() or "banned" in str(exc).lower():
            return None
        return []

    entries: list[dict[str, Any]] = []

    # ── Éléments de conditionnement (étiquettes, capsules, cartons) ──
    for elem in detail.get("elementsConditionnement") or []:
        mp = elem.get("elementMatierePremiere") or {}
        id_mp = mp.get("idMatierePremiere")
        if id_mp is None:
            continue
        qty = float(elem.get("quantite", 0) or 0)
        if qty <= 0:
            continue
        mp_label = (mp.get("libelle") or "").strip()

        entries.append({
            "id_produit": id_produit,
            "format_code": format_code,
            "product_label": product_label,
            "id_mp": id_mp,
            "mp_label": mp_label,
            "qty_per_unit": qty,
            "validated": False,
            "source": "auto_detected",
        })
        _log.info(
            "EasyBeer BOM: %s %s → %s (qty=%.0f)",
            product_label, format_code, mp_label, qty,
        )

    # ── Bouteille (contenant) — extraite du champ séparé du détail stock ──
    contenant = detail.get("contenant") or {}
    cont_id = contenant.get("idContenant")
    cont_libelle = (contenant.get("libelle") or "").strip()
    if cont_id and cont_libelle and lot_qty > 0:
        entries.append({
            "id_produit": id_produit,
            "format_code": format_code,
            "product_label": product_label,
            "id_mp": cont_id,
            "mp_label": cont_libelle,
            "qty_per_unit": lot_qty,
            "validated": False,
            "source": "auto_detected",
        })
        _log.info(
            "EasyBeer BOM bouteille: %s %s → %s (qty=%d)",
            product_label, format_code, cont_libelle, lot_qty,
        )

    return entries


# ─── Product formats (still useful for the nomenclatures page) ────────────

def detect_product_formats_from_stocks(
    produits_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build product-format list from POST /stock/produits data.

    Returns::

        [
            {
                "id_produit": 42,
                "libelle": "Kéfir Gingembre",
                "formats": [
                    {"format_code": "12x33", "contenance": 0.33, "lot_qty": 12},
                ]
            },
            ...
        ]
    """
    if produits_data is None:
        produits_data = _fetch_stock_produits()

    stock_map = _build_stock_map(produits_data)

    # Group by id_produit
    by_product: dict[int, dict[str, Any]] = {}
    for (pid, fmt), info in stock_map.items():
        if pid not in by_product:
            by_product[pid] = {
                "id_produit": pid,
                "libelle": info["libelle"],
                "formats": {},
            }
        by_product[pid]["formats"][fmt] = {
            "format_code": fmt,
            "contenance": info["contenance"],
            "lot_qty": info["lot_qty"],
        }

    result: list[dict[str, Any]] = []
    for pid, data in sorted(by_product.items()):
        result.append({
            "id_produit": pid,
            "libelle": data["libelle"],
            "formats": sorted(data["formats"].values(), key=lambda f: f["format_code"]),
        })

    result.sort(key=lambda r: r["libelle"])
    return result


# ─── Full detection orchestrator ───────────────────────────────────────────

def run_full_detection(tenant_id: str | None = None) -> tuple[int, int]:
    """Run full BOM auto-detection from EasyBeer stock data.

    1. Fetch finished-product stock list (POST /stock/produits)
    2a. For each product-format, fetch conditioning elements via stock detail
    2b. Auto-detect bottles from format codes
    2c. Auto-detect recipe ingredients (jus, sucre, arômes) from product recipes
    3. Bulk upsert into DB (without overwriting validated or conditioning entries)

    Returns ``(total_detected, products_detected)``.
    """
    from common.product_bom import bulk_upsert_bom

    _log.info("Starting full BOM detection from EasyBeer...")

    # 1. Fetch all finished-product stocks
    produits_data = _fetch_stock_produits()
    stock_map = _build_stock_map(produits_data)
    _log.info("Found %d product-formats in EasyBeer stock", len(stock_map))

    # 2. For each product-format, fetch conditioning elements
    all_entries: list[dict[str, Any]] = []
    products_seen: set[int] = set()

    from common.easybeer._client import is_rate_limited

    for (id_produit, fmt), info in sorted(stock_map.items()):
        # Check rate-limit before each API call
        if is_rate_limited() > 0:
            _log.warning(
                "Rate-limit actif, arrêt détection BOM (%d entries, %d produits)",
                len(all_entries), len(products_seen),
            )
            break

        entries = _detect_from_stock_detail(
            id_produit=id_produit,
            product_label=info["libelle"],
            format_code=fmt,
            id_stock_produit=info["sid"],
            lot_qty=info.get("lot_qty", 0),
        )
        if entries:
            all_entries.extend(entries)
            products_seen.add(id_produit)
        elif entries is None:
            # Rate-limited — stop fetching, save what we have
            _log.warning("Rate-limited, stopping BOM detection early")
            break

    # 2b. Fetch all MP once (réutilisé par bottles + recettes)
    from common.easybeer.stocks import get_all_matieres_premieres

    all_mp = get_all_matieres_premieres() or []

    # 2c. Auto-detect bottles (CONTENANT) from format codes
    #     12x33 → 12 × Bouteille 33cl, 6x75 → 6 × Bouteille 75cl
    bottle_entries = _detect_bottles_from_formats(stock_map, all_mp)
    if bottle_entries:
        all_entries.extend(bottle_entries)
        for be in bottle_entries:
            products_seen.add(be["id_produit"])

    # 2d. Auto-detect recipe ingredients (jus, sucre, arômes, etc.)
    #     Uses product recipes to map MP → qty per carton
    mp_ids = {mp["idMatierePremiere"] for mp in all_mp if mp.get("idMatierePremiere")}

    # Cache recettes par produit (évite des appels API doublons si 2+ formats)
    recipe_cache: dict[int, dict[str, Any] | None] = {}
    products_fetched: set[int] = set()
    # Produits sans recette (pour fallback dérivés → parent)
    products_without_recipe: list[tuple[tuple[int, str], dict[str, Any]]] = []

    for (id_produit, fmt), info in sorted(stock_map.items()):
        if is_rate_limited() > 0:
            _log.warning("Rate-limit actif, arrêt détection recette")
            break

        products_fetched.add(id_produit)
        recipe_entries = _detect_from_recipe(
            id_produit=id_produit,
            product_label=info["libelle"],
            format_code=fmt,
            contenance=info["contenance"],
            lot_qty=info["lot_qty"],
            mp_ids=mp_ids,
            recipe_cache=recipe_cache,
        )
        if recipe_entries:
            all_entries.extend(recipe_entries)
            products_seen.add(id_produit)
        else:
            products_without_recipe.append(((id_produit, fmt), info))

    # 2e. Fallback pour produits dérivés sans recette (Niko, Inter, Water)
    #     Utilise le flavor_map pour trouver le produit Symbiose du même goût,
    #     puis copie sa recette avec le bon ratio (contenance × lot_qty).
    if products_without_recipe:
        fallback_entries = _detect_recipe_from_parent(
            products_without_recipe, stock_map, mp_ids, recipe_cache,
        )
        if fallback_entries:
            all_entries.extend(fallback_entries)
            for fe in fallback_entries:
                products_seen.add(fe["id_produit"])

    _log.info("Recipe detection: %d products fetched", len(products_fetched))

    # 3. Bulk upsert (respects existing validated/conditioning entries)
    if all_entries:
        bulk_upsert_bom(all_entries, tenant_id=tenant_id)

    _log.info(
        "BOM detection complete: %d entries for %d products",
        len(all_entries), len(products_seen),
    )
    return len(all_entries), len(products_seen)


def _clean_eb_label(label: str) -> str:
    """Nettoie le libellé EasyBeer : supprime le suffixe degré (ex. '- 0.0°')."""
    import re
    return re.sub(r"\s*-\s*\d+[\.,]?\d*\s*°?\s*$", "", label.strip()).strip()


def _detect_recipe_from_parent(
    products_without_recipe: list[tuple[tuple[int, str], dict[str, Any]]],
    stock_map: dict[tuple[int, str], dict[str, Any]],
    mp_ids: set[int],
    recipe_cache: dict[int, dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """Fallback : copier la recette d'un produit Symbiose parent pour les dérivés.

    Les produits Niko / Inter / Water n'ont pas de recette propre dans EasyBeer.
    On utilise le ``flavor_map.csv`` pour trouver le goût canonique, puis on cherche
    un produit Symbiose avec le même goût qui possède une recette dans le cache.

    Les quantités sont recalculées avec le ratio (contenance × lot_qty) du dérivé.
    """
    from common.data import read_flavor_map

    fm = read_flavor_map()
    if fm.empty:
        _log.warning("flavor_map vide — impossible de résoudre les produits dérivés")
        return []

    # Construire label → canonical (case-insensitive, sans suffixe degré)
    label_to_canonical: dict[str, str] = {}
    for _, row in fm.iterrows():
        name = str(row.get("name", "")).strip().lower()
        canon = str(row.get("canonical", "")).strip()
        if name and canon:
            label_to_canonical[name] = canon

    def _resolve_canon(libelle: str) -> str:
        """Résout le goût canonique depuis un libellé EasyBeer (avec ou sans suffixe °)."""
        lbl = libelle.strip().lower()
        # Essai direct
        canon = label_to_canonical.get(lbl, "")
        if canon:
            return canon
        # Essai sans le suffixe degré (ex: "NIKO - Kéfir de fruits Gingembre - 0.0°" → sans " - 0.0°")
        cleaned = _clean_eb_label(lbl)
        return label_to_canonical.get(cleaned, "")

    # Construire canonical → id_produit pour les produits QUI ONT une recette.
    # D'abord chercher dans le cache, puis fetch les parents manquants.
    canonical_to_parent: dict[str, int] = {}
    # Passe 1 : produits déjà en cache
    for (pid, _fmt), info in stock_map.items():
        canon = _resolve_canon(info["libelle"])
        if not canon:
            continue
        cached = recipe_cache.get(pid)
        if cached and (cached.get("recettes") or []):
            canonical_to_parent.setdefault(canon, pid)

    # Passe 2 : pour les goûts des dérivés sans parent dans le cache,
    # fetch la recette du produit Symbiose correspondant à la volée
    needed_canons = set()
    for (id_produit, fmt), info in products_without_recipe:
        canon = _resolve_canon(info["libelle"])
        if canon and canon not in canonical_to_parent:
            needed_canons.add(canon)

    if needed_canons:
        from common.easybeer.products import get_product_detail

        for (pid, _fmt), info in stock_map.items():
            canon = _resolve_canon(info["libelle"])
            if canon not in needed_canons or canon in canonical_to_parent:
                continue
            # Ce produit pourrait être un parent — tenter le fetch
            if pid not in recipe_cache:
                try:
                    detail = get_product_detail(pid)
                    recipe_cache[pid] = detail
                except Exception as exc:
                    _log.warning("Fetch recette parent %s (#%d): %s", info["libelle"], pid, exc)
                    recipe_cache[pid] = None
                    continue
            cached = recipe_cache.get(pid)
            if cached and (cached.get("recettes") or []):
                canonical_to_parent[canon] = pid
                _log.info("Parent trouvé pour '%s': %s (#%d)", canon, info["libelle"], pid)

    _log.info(
        "Fallback recette: %d produits sans recette, %d goûts parents disponibles (canons: %s)",
        len(products_without_recipe), len(canonical_to_parent),
        ", ".join(sorted(canonical_to_parent.keys())) or "aucun",
    )

    entries: list[dict[str, Any]] = []
    for (id_produit, fmt), info in products_without_recipe:
        canon = _resolve_canon(info["libelle"])
        if not canon:
            _log.debug("Pas de goût canonique pour %s — skip", info["libelle"])
            continue
        parent_pid = canonical_to_parent.get(canon)
        if not parent_pid:
            _log.debug("Pas de parent pour goût '%s' (%s) — skip", canon, info["libelle"])
            continue

        # Utiliser la recette du parent avec le ratio du dérivé
        parent_detail = recipe_cache.get(parent_pid)
        if not parent_detail:
            continue
        recettes = parent_detail.get("recettes") or []
        if not recettes:
            continue
        recette = recettes[0]
        volume_recette = float(recette.get("volumeRecette", 0) or 0)
        if volume_recette <= 0:
            continue

        contenance = info["contenance"]
        lot_qty = info["lot_qty"]
        carton_volume = lot_qty * contenance
        ratio = carton_volume / volume_recette

        for ing in recette.get("ingredients") or []:
            mp = ing.get("matierePremiere") or {}
            id_mp = mp.get("idMatierePremiere")
            if not id_mp or id_mp not in mp_ids:
                continue
            qty_recipe = float(ing.get("quantite", 0) or 0)
            if qty_recipe <= 0:
                continue

            qty_per_carton = round(qty_recipe * ratio, 4)
            mp_label = (mp.get("libelle") or "").strip()

            entries.append({
                "id_produit": id_produit,
                "format_code": fmt,
                "product_label": info["libelle"],
                "id_mp": id_mp,
                "mp_label": mp_label,
                "qty_per_unit": qty_per_carton,
                "validated": False,
                "source": "recipe_api",
            })

        if entries and entries[-1]["id_produit"] == id_produit:
            _log.info(
                "BOM recette fallback: %s %s → copié depuis parent (goût '%s')",
                info["libelle"], fmt, canon,
            )

    return entries


def _detect_from_recipe(
    id_produit: int,
    product_label: str,
    format_code: str,
    contenance: float,
    lot_qty: int,
    mp_ids: set[int],
    recipe_cache: dict[int, dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """Extract recipe ingredients as BOM entries for a product-format.

    Uses ``get_product_detail()`` to read the product recipe, then converts
    each ingredient quantity from the recipe reference volume to qty per carton.

    Only includes ingredients whose ``idMatierePremiere`` is in *mp_ids*
    (i.e. tracked in EasyBeer stock — excludes water, etc.).

    *recipe_cache* avoids duplicate API calls for products with multiple formats.

    Formula::

        qty_per_carton = (ingredient.quantite / volumeRecette) × (lot_qty × contenance)

    Returns BOM entry dicts with ``source="recipe_api"``.
    """
    # Utiliser le cache si disponible
    if recipe_cache is not None and id_produit in recipe_cache:
        detail = recipe_cache[id_produit]
        if detail is None:
            return []  # Échec précédent mis en cache
    else:
        from common.easybeer.products import get_product_detail
        try:
            detail = get_product_detail(id_produit)
        except Exception as exc:
            _log.warning(
                "Cannot fetch product detail %d for recipe BOM: %s",
                id_produit, exc,
            )
            if recipe_cache is not None:
                recipe_cache[id_produit] = None
            return []
        if recipe_cache is not None:
            recipe_cache[id_produit] = detail

    recettes = detail.get("recettes") or []
    if not recettes:
        return []

    recette = recettes[0]
    volume_recette = float(recette.get("volumeRecette", 0) or 0)
    if volume_recette <= 0:
        return []

    carton_volume = lot_qty * contenance  # litres per carton
    ratio = carton_volume / volume_recette

    entries: list[dict[str, Any]] = []
    for ing in recette.get("ingredients") or []:
        mp = ing.get("matierePremiere") or {}
        id_mp = mp.get("idMatierePremiere")
        if not id_mp or id_mp not in mp_ids:
            continue

        qty_recipe = float(ing.get("quantite", 0) or 0)
        if qty_recipe <= 0:
            continue

        qty_per_carton = round(qty_recipe * ratio, 4)
        mp_label = (mp.get("libelle") or "").strip()

        entries.append({
            "id_produit": id_produit,
            "format_code": format_code,
            "product_label": product_label,
            "id_mp": id_mp,
            "mp_label": mp_label,
            "qty_per_unit": qty_per_carton,
            "validated": False,
            "source": "recipe_api",
        })
        _log.info(
            "BOM recette: %s %s → %s (qty=%.4f, ratio=%.6f)",
            product_label, format_code, mp_label, qty_per_carton, ratio,
        )

    return entries


def _index_bottle(
    bottle_by_key: dict[str, dict[str, Any]],
    mp_id: int,
    label: str,
) -> None:
    """Classe une bouteille dans bottle_by_key par contenance/type."""
    label_lower = label.lower()
    if "0.33" in label_lower or "33cl" in label_lower:
        bottle_by_key.setdefault("33cl", {"id_mp": mp_id, "label": label})
    elif "saft" in label_lower and ("0.75" in label_lower or "75cl" in label_lower):
        bottle_by_key.setdefault("75cl_saft", {"id_mp": mp_id, "label": label})
    elif "eau" in label_lower and ("0.75" in label_lower or "75cl" in label_lower):
        bottle_by_key.setdefault("75cl_eau", {"id_mp": mp_id, "label": label})


def _detect_bottles_from_formats(
    stock_map: dict[tuple[int, str], dict[str, Any]],
    all_mp: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Auto-detect bottle (CONTENANT) BOM entries from product formats.

    Mapping rules (contenance + lot_qty + marque) :
    - 33cl (tout format)      → "Bouteille - 0.33L"  (Bavarian, Wiegand-glas)
    - 75cl + 4x  + Symbiose   → "Bouteille 75cl SAFT - 0.75L"  (Wiegand-glas)
    - 75cl + 6x  + Symbiose   → "Bouteille 75cl EAU GAZEUSE - 0.75L" (Verallia)
    - 75cl + 6x  + Niko       → "Bouteille 75cl SAFT - 0.75L"  (Wiegand-glas)

    The quantity per carton equals the lot_qty (1 bouteille par unité).
    """
    # ── Index des bouteilles par label normalisé ──
    # Accepte CONTENANT (type principal) et CONDITIONNEMENT (fallback si CONTENANT absent)
    _BOTTLE_TYPES = {"CONTENANT", "CONDITIONNEMENT"}
    bottle_by_key: dict[str, dict[str, Any]] = {}

    # Diagnostic : compter les types MP pour identifier le bon filtre
    type_counts: dict[str, int] = {}
    for mp in all_mp:
        mp_type = (mp.get("type") or {}).get("code", "")
        type_counts[mp_type] = type_counts.get(mp_type, 0) + 1

    _log.info("Types MP EasyBeer: %s", type_counts)

    # Passe 1 : chercher dans CONTENANT uniquement
    for mp in all_mp:
        mp_type = (mp.get("type") or {}).get("code", "")
        label = (mp.get("libelle") or "").strip()
        mp_id = mp.get("idMatierePremiere")
        if mp_type != "CONTENANT" or not mp_id:
            continue
        _index_bottle(bottle_by_key, mp_id, label)

    # Passe 2 : si aucun CONTENANT trouvé, fallback sur CONDITIONNEMENT
    # en cherchant les MP dont le libellé contient "bouteille"
    if not bottle_by_key:
        _log.warning("Aucune MP de type CONTENANT — fallback sur CONDITIONNEMENT avec filtre 'bouteille'")
        for mp in all_mp:
            mp_type = (mp.get("type") or {}).get("code", "")
            label = (mp.get("libelle") or "").strip()
            mp_id = mp.get("idMatierePremiere")
            if mp_type not in _BOTTLE_TYPES or not mp_id:
                continue
            if "bouteille" not in label.lower():
                continue
            _index_bottle(bottle_by_key, mp_id, label)

    _log.info("Bouteilles indexées: %s", {k: v["label"] for k, v in bottle_by_key.items()})

    if not bottle_by_key:
        _log.warning(
            "Aucune bouteille trouvée dans les MP EasyBeer (types disponibles: %s)",
            ", ".join(sorted(type_counts.keys())),
        )
        return []

    # ── Détection de la marque par le libellé produit ──
    def _is_niko(product_label: str) -> bool:
        return "niko" in product_label.lower()

    # ── Résolution bouteille par (contenance, lot_qty, marque) ──
    def _resolve_bottle(
        contenance_cl: int, lot_qty: int, product_label: str,
    ) -> dict[str, Any] | None:
        if contenance_cl == 33:
            return bottle_by_key.get("33cl")
        if contenance_cl == 75:
            if _is_niko(product_label):
                # Niko 75cl → toujours Saft
                return bottle_by_key.get("75cl_saft")
            # Symbiose 75cl : 4x = Saft, 6x = Eau gazeuse
            if lot_qty <= 4:
                return bottle_by_key.get("75cl_saft")
            return bottle_by_key.get("75cl_eau")
        return None

    entries: list[dict[str, Any]] = []
    for (id_produit, fmt), info in stock_map.items():
        contenance_cl = int(info["contenance"] * 100)
        lot_qty = info["lot_qty"]
        bottle = _resolve_bottle(contenance_cl, lot_qty, info["libelle"])
        if not bottle:
            _log.debug(
                "Pas de bouteille pour %dcl lot=%d %s (%s)",
                contenance_cl, lot_qty, info["libelle"], fmt,
            )
            continue

        entries.append({
            "id_produit": id_produit,
            "format_code": fmt,
            "product_label": info["libelle"],
            "id_mp": bottle["id_mp"],
            "mp_label": bottle["label"],
            "qty_per_unit": lot_qty,
            "validated": False,
            "source": "auto_detected",
        })
        _log.info(
            "BOM bouteille: %s %s → %s (qty=%d)",
            info["libelle"], fmt, bottle["label"], lot_qty,
        )

    return entries
