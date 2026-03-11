"""
common/sync/collector.py
========================
Collecteur de données étiquetage depuis EasyBeer.

Interroge les brassins en cours, produits dérivés, matrice codes-barres et
stocks produit fini pour construire la liste complète des produits à
synchroniser vers la base Access de l'imprimante d'étiquettes.

Rate-limiting EasyBeer :
  - Délai global de 200ms entre appels (via _client._throttle)
  - Délai supplémentaire de 1.5s entre appels stock/produit/edition
  - Cache fichier 24h pour les codes articles (évite les appels répétés)
  - Détection de ban (429/400) : arrêt immédiat de la boucle
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time
from typing import Any

from common.easybeer.brassins import get_brassin_detail, get_brassins_en_cours
from common.easybeer.conditioning import get_code_barre_matrice
from common.easybeer.products import get_product_detail
from common.easybeer.stocks import get_stock_produit_detail
from common.easybeer._client import BASE, _auth, _check_response, _safe_json, get_session, TIMEOUT, EasyBeerError
from common.ramasse import clean_product_label

_log = logging.getLogger("ferment.sync")

# DDM par défaut = date brassage + 365 jours
_DDM_DAYS = 365

# Délai entre chaque appel stock/produit/edition (secondes)
_STOCK_DETAIL_DELAY = 1.5

# Délai entre les groupes d'appels API (cooldown, secondes)
_API_GROUP_COOLDOWN = 2.0


# ─── Cache fichier codes articles (24h) ──────────────────────────────────────

_STOCK_CODES_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "_stock_codes_cache.json",
)
_STOCK_CODES_CACHE_TTL = 24 * 3600  # 24 heures


def _load_stock_codes_cache() -> dict[tuple[int, str], str] | None:
    """Charge le cache fichier des codes articles si encore valide."""
    try:
        with open(_STOCK_CODES_CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
        ts = cache.get("ts", 0)
        if dt.datetime.now(dt.UTC).timestamp() - ts > _STOCK_CODES_CACHE_TTL:
            _log.debug("Cache codes articles expiré")
            return None
        codes: dict[tuple[int, str], str] = {}
        for entry in cache.get("data", []):
            codes[(entry["pid"], entry["fmt"])] = entry["code"]
        _log.info("Cache codes articles valide : %d entrées", len(codes))
        return codes
    except (OSError, ValueError, KeyError):
        _log.debug("Pas de cache codes articles ou erreur lecture", exc_info=True)
        return None


def _save_stock_codes_cache(codes: dict[tuple[int, str], str]) -> None:
    """Sauvegarde le cache fichier des codes articles."""
    data = [{"pid": pid, "fmt": fmt, "code": code} for (pid, fmt), code in codes.items()]
    cache = {"ts": dt.datetime.now(dt.UTC).timestamp(), "data": data}
    try:
        cache_dir = os.path.dirname(_STOCK_CODES_CACHE_PATH)
        os.makedirs(cache_dir, exist_ok=True)
        # Écriture atomique via fichier temporaire + rename
        tmp_path = _STOCK_CODES_CACHE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp_path, _STOCK_CODES_CACHE_PATH)
        _log.info("Cache codes articles sauvegardé : %d entrées", len(codes))
    except (OSError, ValueError):
        _log.warning("Impossible de sauvegarder le cache codes articles", exc_info=True)


# ─── Étape 1 : Brassins en cours ────────────────────────────────────────────


def _fetch_active_brassins() -> list[dict[str, Any]]:
    """Récupère les brassins en cours, filtre annulés/terminés/trop petits."""
    brassins = get_brassins_en_cours()
    active = []
    for b in brassins:
        if b.get("annule") or b.get("termine"):
            continue
        vol = float(b.get("volume") or 0)
        if vol < 100:
            continue
        active.append(b)
    _log.info("Brassins en cours : %d actifs sur %d total", len(active), len(brassins))
    return active


# ─── Étape 2 : DDM et Lot depuis le détail brassin ──────────────────────────


def _compute_ddm(detail: dict[str, Any]) -> dt.date:
    """Calcule la DDM = date début brassin + 365 jours.

    Le champ dateDebutFormulaire est un timestamp ms (int/float) ou une string ISO.
    Fallback : date du jour + 365 jours.
    """
    raw_debut = detail.get("dateDebutFormulaire")
    if not raw_debut:
        _log.warning("Brassin sans dateDebutFormulaire, fallback DDM=today+365")
        return dt.date.today() + dt.timedelta(days=_DDM_DAYS)
    try:
        if isinstance(raw_debut, (int, float)):
            base = dt.date.fromtimestamp(raw_debut / 1000)
        else:
            base = dt.date.fromisoformat(str(raw_debut)[:10])
        return base + dt.timedelta(days=_DDM_DAYS)
    except (ValueError, TypeError, OSError):
        _log.warning("Impossible de parser dateDebutFormulaire=%r, fallback", raw_debut)
        return dt.date.today() + dt.timedelta(days=_DDM_DAYS)


def _ddm_to_lot(ddm: dt.date) -> int:
    """Formate la DDM en numéro de lot DDMMYYYY (ex: 11032027)."""
    return int(ddm.strftime("%d%m%Y"))


# ─── Étape 3 : Produit + dérivés ────────────────────────────────────────────

# Cache en mémoire des détails produit (reset à chaque cycle de collecte)
_product_cache: dict[int, dict[str, Any]] = {}


def _get_product_detail_cached(id_produit: int) -> dict[str, Any]:
    """get_product_detail() avec cache mémoire intra-cycle."""
    if id_produit in _product_cache:
        return _product_cache[id_produit]
    detail = get_product_detail(id_produit)
    _product_cache[id_produit] = detail
    time.sleep(0.5)  # Petit délai entre appels produit
    return detail


def _get_all_product_ids(id_produit: int) -> list[int]:
    """Retourne [produit principal] + tous les produits dérivés (NIKO, Export, etc.)."""
    ids = [id_produit]
    try:
        detail = _get_product_detail_cached(id_produit)
        derived = detail.get("idsProduitsDerives") or []
        for did in derived:
            if isinstance(did, int) and did not in ids:
                ids.append(did)
    except Exception:
        _log.warning("Impossible de charger les dérivés du produit %s", id_produit, exc_info=True)
    return ids


# ─── Étape 4 : Matrice codes-barres → GTIN + PCB ────────────────────────────


def _parse_barcode_matrix_for_labels(
    raw_matrice: dict[str, Any],
) -> dict[int, dict[str, dict[str, Any]]]:
    """Parse la matrice codes-barres et organise par produit et format.

    Retourne :
        {idProduit: {fmt_str: {"gtin_uvc": str, "gtin_colis": str, "pcb": int}}}

    Distinction GTIN UVC / GTIN Colis :
      - modeleLot.quantite == 1  → GTIN UVC (bouteille individuelle)
      - modeleLot.quantite > 1   → GTIN Colis (carton), PCB = quantite
    """
    by_product: dict[int, dict[str, dict[str, Any]]] = {}

    for prod_entry in raw_matrice.get("produits", []):
        for cb in prod_entry.get("codesBarres", []):
            code_raw = str(cb.get("code") or "").strip()
            id_produit = (cb.get("modeleProduit") or {}).get("idProduit")
            mod_cont = cb.get("modeleContenant") or {}
            contenance = round(float(mod_cont.get("contenance") or 0), 2)
            mod_lot = cb.get("modeleLot") or {}
            lot_libelle = (mod_lot.get("libelle") or "").strip()
            lot_qty = int(mod_lot.get("quantite") or 0)

            if not (id_produit and code_raw and contenance):
                continue

            digits = re.sub(r"\D+", "", code_raw)
            if len(digits) < 8:  # EAN8 minimum
                continue

            vol_cl = int(contenance * 100)  # 0.33 → 33, 0.75 → 75

            if lot_qty <= 0:
                # Essai extraction depuis le libellé (ex: "Carton de 12")
                m = re.search(r"(\d+)", lot_libelle)
                if m:
                    lot_qty = int(m.group(1))
                else:
                    continue

            if lot_qty == 1:
                # C'est un GTIN UVC (bouteille individuelle)
                # On ne connaît pas encore le format carton, on stocke temporairement
                # Le fmt_str sera associé via le volume
                for fmt_str, info in by_product.get(id_produit, {}).items():
                    # Matcher sur le volume (même contenance)
                    fmt_vol = re.search(r"x(\d+)", fmt_str)
                    if fmt_vol and int(fmt_vol.group(1)) == vol_cl:
                        info["gtin_uvc"] = digits
                # Stocker aussi en clé spéciale pour association ultérieure
                by_product.setdefault(id_produit, {})
                by_product[id_produit].setdefault(f"_uvc_{vol_cl}", {})["gtin_uvc"] = digits
            else:
                # C'est un GTIN Colis (carton)
                fmt_str = f"{lot_qty}x{vol_cl}"
                by_product.setdefault(id_produit, {})
                entry = by_product[id_produit].setdefault(fmt_str, {
                    "gtin_uvc": "",
                    "gtin_colis": "",
                    "pcb": lot_qty,
                })
                entry["gtin_colis"] = digits
                entry["pcb"] = lot_qty

    # Deuxième passe : associer les GTIN UVC aux formats
    for id_produit, formats in by_product.items():
        uvc_keys = [k for k in formats if k.startswith("_uvc_")]
        for uvc_key in uvc_keys:
            vol_cl_str = uvc_key.split("_")[-1]
            gtin_uvc = formats[uvc_key].get("gtin_uvc", "")
            if gtin_uvc:
                for fmt_str, info in formats.items():
                    if fmt_str.startswith("_"):
                        continue
                    fmt_vol = re.search(r"x(\d+)", fmt_str)
                    if fmt_vol and fmt_vol.group(1) == vol_cl_str and not info.get("gtin_uvc"):
                        info["gtin_uvc"] = gtin_uvc
            del formats[uvc_key]

    return by_product


# ─── Étape 5 : CODE INTERNE (codeArticle) depuis stocks ─────────────────────


def _is_ban_error(exc: Exception) -> bool:
    """Détecte si l'erreur est un ban rate-limit EasyBeer (429 ou 'banned')."""
    msg = str(exc).lower()
    return any(kw in msg for kw in ("429", "banned", "too many", "rate limit"))


def _fetch_stock_codes() -> dict[tuple[int, str], str]:
    """Récupère les codeArticle depuis POST /stock/produits.

    Retourne {(idProduit, fmt_str): codeArticle}.

    Utilise un cache fichier 24h pour éviter les appels API répétés.
    En cas de rate-limit ou ban, retourne ce qui a été collecté jusque-là.
    """
    # 1. Essayer le cache fichier d'abord
    cached = _load_stock_codes_cache()
    if cached is not None:
        return cached

    _log.info("Fetch codes articles depuis EasyBeer (cache absent ou expiré)")

    id_brasserie = int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013"))
    payload = {"idBrasserie": id_brasserie}

    try:
        r = get_session().post(
            f"{BASE}/stock/produits",
            json=payload,
            auth=_auth(),
            timeout=TIMEOUT,
        )
        _check_response(r, "stock/produits")
        data = _safe_json(r, "stock/produits")
    except (EasyBeerError, Exception):
        _log.exception("Erreur fetch stock/produits pour codes articles")
        return {}

    codes: dict[tuple[int, str], str] = {}
    total_items = 0
    ban_detected = False

    # Compter d'abord le nombre d'items pour le logging
    items_to_fetch = []
    for prod in data.get("consolidationsFilles", []):
        for conso in prod.get("consolidationsFilles", []):
            sid = conso.get("id")
            if not sid:
                continue
            produit = conso.get("produit") or {}
            id_produit = produit.get("idProduit")
            lot = conso.get("lot") or {}
            cont = conso.get("contenant") or {}
            contenance = float(cont.get("contenance", 0) or 0)
            lot_qty = int(lot.get("quantite", 0) or 0)
            if not (id_produit and contenance and lot_qty):
                continue
            fmt_str = f"{lot_qty}x{int(contenance * 100)}"
            items_to_fetch.append((sid, id_produit, fmt_str))

    _log.info("Stock codes : %d items à interroger", len(items_to_fetch))

    for i, (sid, id_produit, fmt_str) in enumerate(items_to_fetch):
        try:
            detail = get_stock_produit_detail(sid)
            code_article = (detail.get("codeArticle") or "").strip()
            if code_article:
                codes[(id_produit, fmt_str)] = code_article
            total_items += 1
        except (EasyBeerError, Exception) as exc:
            if _is_ban_error(exc):
                _log.error(
                    "Rate-limit/ban EasyBeer détecté après %d/%d items : %s. "
                    "Arrêt de la boucle, on continue avec %d codes collectés.",
                    i, len(items_to_fetch), exc, len(codes),
                )
                ban_detected = True
                break
            _log.warning("Erreur fetch detail stock %s : %s", sid, exc)

        # Délai entre chaque appel — plus long que l'ancien 0.3s
        time.sleep(_STOCK_DETAIL_DELAY)

    _log.info("Codes articles récupérés : %d entrées sur %d items", len(codes), total_items)

    # Sauvegarder en cache même si incomplet (si ban, on a au moins une partie)
    if codes and not ban_detected:
        _save_stock_codes_cache(codes)
    elif codes and ban_detected:
        _log.warning("Cache non sauvegardé (collecte incomplète à cause d'un ban)")

    return codes


# ─── Étape 6 : Assemblage ────────────────────────────────────────────────────


def _determine_brand(product_label: str) -> str:
    """Détermine la marque à partir du libellé produit.

    Si le nom contient 'niko' → NIKO, sinon SYMBIOSE.
    """
    if "niko" in product_label.lower():
        return "NIKO"
    return "SYMBIOSE"


def collect_label_data() -> list[dict[str, Any]]:
    """Point d'entrée principal : collecte tous les produits pour la sync étiquettes.

    Pour chaque brassin en cours :
      1. Calcule DDM et Lot
      2. Identifie le produit principal + dérivés
      3. Pour chaque produit × format, assemble les champs Access

    Retourne une liste de dicts prêts pour la table Access "Produits".
    """
    _log.info("=== Début collecte données étiquettes ===")

    # Reset du cache produit en mémoire pour ce cycle
    _product_cache.clear()

    # Étape 1 : Brassins en cours
    brassins = _fetch_active_brassins()
    if not brassins:
        _log.warning("Aucun brassin en cours, sync vide")
        return []

    time.sleep(_API_GROUP_COOLDOWN)

    # Étape 5 : Codes articles (le plus coûteux en appels, fait en premier)
    # Si le cache fichier est valide, c'est instantané
    stock_codes = _fetch_stock_codes()

    time.sleep(_API_GROUP_COOLDOWN)

    # Étape 4 : Matrice codes-barres (appel unique, global)
    try:
        raw_matrice = get_code_barre_matrice()
        cb_by_product = _parse_barcode_matrix_for_labels(raw_matrice)
    except Exception:
        _log.exception("Erreur chargement matrice codes-barres")
        cb_by_product = {}

    time.sleep(_API_GROUP_COOLDOWN)

    # Étape 2+3+6 : Pour chaque brassin, assembler les produits
    products: list[dict[str, Any]] = []
    seen: set[str] = set()  # Dédoublonnage par (code_interne, lot)

    for brassin in brassins:
        id_brassin = brassin.get("idBrassin")
        brassin_produit = brassin.get("produit") or {}
        id_produit = brassin_produit.get("idProduit")
        brassin_nom = brassin.get("nom") or f"Brassin {id_brassin}"

        if not id_brassin or not id_produit:
            _log.warning("Brassin sans idBrassin ou idProduit, skip: %s", brassin_nom)
            continue

        # Détail brassin → DDM
        try:
            detail = get_brassin_detail(id_brassin)
        except Exception:
            _log.warning("Impossible de charger détail brassin %s, skip", id_brassin, exc_info=True)
            continue

        ddm = _compute_ddm(detail)
        lot = _ddm_to_lot(ddm)
        time.sleep(0.5)  # Petit cooldown entre brassin detail et product detail

        # Produit principal + dérivés
        all_product_ids = _get_all_product_ids(id_produit)
        _log.debug("Brassin %s : produit %s + %d dérivé(s)", brassin_nom, id_produit, len(all_product_ids) - 1)

        for pid in all_product_ids:
            # Récupérer le libellé du produit (avec cache mémoire)
            try:
                prod_detail = _get_product_detail_cached(pid)
                prod_label = (prod_detail.get("libelle") or "").strip()
            except Exception:
                _log.warning("Impossible de charger produit %s", pid, exc_info=True)
                continue

            if not prod_label:
                continue

            clean_label = clean_product_label(prod_label)
            marque = _determine_brand(prod_label)

            # Formats disponibles depuis la matrice codes-barres
            formats = cb_by_product.get(pid, {})
            if not formats:
                _log.debug("Produit %s (%s) : aucun format dans la matrice CB", pid, clean_label)
                continue

            for fmt_str, cb_info in formats.items():
                code_interne = stock_codes.get((pid, fmt_str), "")

                # CODE INTERNE obligatoire (NOT NULL dans la table Access Domino)
                if not code_interne:
                    _log.debug(
                        "Produit %s (%s) format %s : pas de code_interne, skip",
                        pid, clean_label, fmt_str,
                    )
                    continue

                gtin_uvc = cb_info.get("gtin_uvc", "")
                gtin_colis = cb_info.get("gtin_colis", "")
                pcb = cb_info.get("pcb", 0)

                # Construire la désignation (ex: "Kéfir Gingembre — 12x33cl")
                designation = f"{clean_label} — {fmt_str}cl"

                # Dédoublonnage par code_interne + lot
                dedup_key = f"{code_interne}|{lot}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                products.append({
                    "designation": designation,
                    "marque": marque,
                    "code_interne": code_interne,
                    "pcb": float(pcb),
                    "gtin_uvc": gtin_uvc,
                    "gtin_colis": gtin_colis,
                    "lot": float(lot),
                    "ddm": ddm.isoformat(),
                })

    _log.info(
        "=== Collecte terminée : %d produits pour %d brassins ===",
        len(products), len(brassins),
    )
    return products
