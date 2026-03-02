"""
common/easybeer.py
==================
Client centralisé pour l'API Easy Beer (api.easybeer.fr).
Authentification : HTTP Basic Auth (EASYBEER_API_USER / EASYBEER_API_PASS).

Endpoints utilisés :
  POST /indicateur/autonomie-stocks/export/excel  → Excel ventes+stock (01_Accueil)
  POST /indicateur/autonomie-stocks               → JSON autonomie produits finis
  GET  /stock/matieres-premieres/all              → stock tous composants (MP)
  POST /indicateur/synthese-consommations-mp      → consommation MP par période
  POST /stock/contenant/historique                → historique mouvements stock (paginé)
  POST /parametres/client/liste                   → liste paginée des clients
  GET  /parametres/produit/liste/all              → tous les produits
  GET  /parametres/entrepot/liste                 → tous les entrepôts
  POST /brassin/enregistrer                       → créer un brassin
  GET  /brassin/en-cours/liste                    → brassins en cours
  GET  /brassin/{idBrassin}                       → détail complet d'un brassin
"""
from __future__ import annotations

import datetime
import logging
import os
import re
import threading as _threading
import time as _time
from typing import Any

import requests

_log = logging.getLogger("ferment.easybeer")

# ─── Config (variables d'environnement) ────────────────────────────────────────
# NOTE : les credentials sont lus à chaque appel (pas au niveau module)
# pour éviter les problèmes d'ordre de chargement du .env.
BASE            = "https://api.easybeer.fr"
TIMEOUT         = 30  # secondes


def is_configured() -> bool:
    """True si les credentials Easy Beer sont présents."""
    return bool(os.environ.get("EASYBEER_API_USER") and os.environ.get("EASYBEER_API_PASS"))


# ─── Rate-limiter global (thread-safe) ───────────────────────────────────────
# EasyBeer interdit > 10 requêtes/seconde et BAN 5 min si dépassement (HTTP 400).
# On espace chaque appel de min 200ms → max 5 req/s, bien en dessous de la limite.
# Le lock garantit qu'en cas d'appels concurrents, chaque thread attend son tour.
_API_MIN_INTERVAL = 0.2  # secondes
_api_last_ts: float = 0.0
_api_lock = _threading.Lock()


def _throttle() -> None:
    """Espace les appels API de min 200ms pour éviter le ban rate-limit (thread-safe)."""
    global _api_last_ts
    with _api_lock:
        now = _time.monotonic()
        wait = _API_MIN_INTERVAL - (now - _api_last_ts)
        if wait > 0:
            _time.sleep(wait)
        _api_last_ts = _time.monotonic()


def _auth() -> tuple[str, str]:
    _throttle()  # appliqué à chaque appel API (tous passent par _auth)
    return (
        os.environ.get("EASYBEER_API_USER", ""),
        os.environ.get("EASYBEER_API_PASS", ""),
    )


class EasyBeerError(RuntimeError):
    """Erreur lors d'un appel à l'API EasyBeer."""


def _check_response(r: requests.Response, endpoint: str) -> None:
    """Vérifie la réponse HTTP et lève une erreur lisible."""
    if r.ok:
        return
    # Détecter les pages d'erreur HTML (proxy, WAF, serveur en maintenance)
    body = r.text[:500]
    if "<!DOCTYPE" in body or "<html" in body.lower():
        raise EasyBeerError(
            f"EasyBeer {endpoint} → HTTP {r.status_code} : le serveur a renvoyé une page HTML "
            f"(maintenance ou erreur proxy). Réessayez dans quelques minutes."
        )
    raise EasyBeerError(
        f"EasyBeer {endpoint} → HTTP {r.status_code} : {body[:300]}"
    )


def _dates(window_days: int) -> tuple[str, str]:
    """Retourne (date_debut_iso, date_fin_iso) pour une fenetre de N jours jusqu'a aujourd'hui."""
    fin   = datetime.datetime.now(datetime.timezone.utc)
    debut = fin - datetime.timedelta(days=window_days)
    return (
        debut.strftime("%Y-%m-%dT00:00:00.000Z"),
        fin.strftime("%Y-%m-%dT23:59:59.999Z"),
    )


def _base_payload(window_days: int) -> dict[str, Any]:
    """
    Payload commun pour TOUS les endpoints /indicateur/* et /export/excel.
    Le schéma ModeleIndicateur accepte un objet 'periode' avec :
      - dateDebut / dateFin  : bornes de la période
      - type: "PERIODE_LIBRE" : obligatoire pour que l'API interprète les dates
    """
    debut, fin = _dates(window_days)
    return {
        "idBrasserie": int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013")),
        "periode": {
            "dateDebut": debut,
            "dateFin":   fin,
            "type":      "PERIODE_LIBRE",
        },
    }


# Alias pour compatibilité interne
_excel_payload     = _base_payload
_indicator_payload = _base_payload


# ─── Endpoints ─────────────────────────────────────────────────────────────────

def get_autonomie_stocks_excel(window_days: int) -> bytes:
    """
    POST /indicateur/autonomie-stocks/export/excel
    → Bytes du fichier Excel (utilisé par 01_Accueil pour le planning de production).
    """
    r = requests.post(
        f"{BASE}/indicateur/autonomie-stocks/export/excel",
        json=_excel_payload(window_days),
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.content


def get_autonomie_stocks(window_days: int) -> dict[str, Any]:
    """
    POST /indicateur/autonomie-stocks
    → JSON avec autonomie (jours de stock) par produit fini.

    Réponse : ModeleAutonomie
      {
        "codeRetour": "OK",
        "produits": [                          ← ModeleAutonomieProduit[]
          {
            "libelle": "Kéfir Original",
            "quantite": 1200,                  ← stock physique
            "quantiteVirtuelle": 1150,         ← stock virtuel (réservations déduites)
            "volume": 4.0,                     ← hL
            "volumeVirtuel": 3.9,
            "autonomie": 28.5,                 ← JOURS DE STOCK (déjà calculé !)
            "stocksProduits": [...]            ← détail par contenant
          }
        ],
        "stocksAutres": [...]
      }
    """
    r = requests.post(
        f"{BASE}/indicateur/autonomie-stocks",
        params={"forceRefresh": False},
        json=_indicator_payload(window_days),
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "autonomie-stocks")
    return r.json()


def get_mp_lots(id_matiere_premiere: int) -> list[dict[str, Any]]:
    """
    GET /stock/matieres-premieres/numero-lot/liste/{idMatierePremiere}
    → Liste des numéros de lot d'une matière première.

    Chaque élément : ModeleMatierePremiereNumeroLot
      {
        "idMatierePremiereNumeroLot": 123,
        "idMatierePremiere": 42,
        "numeroLot": "AAA2026",
        "quantite": 20.0,
        "dateLimiteUtilisationOptimale": 1703980800000,
        "unite": {...}
      }
    """
    ep = f"matieres-premieres/numero-lot/liste/{id_matiere_premiere}"
    r = requests.get(
        f"{BASE}/stock/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = r.json()
    return data if isinstance(data, list) else []


def get_stock_produit_detail(id_stock_produit: int) -> dict[str, Any]:
    """
    GET /stock/produit/edition/{idStockProduit}
    → Détail complet d'un stock produit, incluant poidsUnitaire.

    Champs utiles :
      - poidsUnitaire     → poids du carton/pack complet (kg)
      - contenant         → {contenance, poidsUnitaire (bouteille vide), ...}
      - lot               → {libelle, quantite}
      - produit           → {idProduit, nom, ...}
    """
    r = requests.get(
        f"{BASE}/stock/produit/edition/{id_stock_produit}",
        auth=_auth(),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


_WEIGHTS_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "_carton_weights_cache.json",
)
_WEIGHTS_CACHE_TTL = 24 * 3600  # 24 heures


def _load_weights_cache() -> dict[tuple[int, str], float] | None:
    """Charge le cache fichier des poids cartons si encore valide."""
    import json
    try:
        with open(_WEIGHTS_CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
        ts = cache.get("ts", 0)
        if datetime.datetime.now(datetime.timezone.utc).timestamp() - ts > _WEIGHTS_CACHE_TTL:
            return None  # expiré
        weights: dict[tuple[int, str], float] = {}
        for entry in cache.get("data", []):
            weights[(entry["pid"], entry["fmt"])] = entry["w"]
        return weights
    except Exception:
        return None


def _save_weights_cache(weights: dict[tuple[int, str], float]) -> None:
    """Sauvegarde le cache fichier des poids cartons (ecriture atomique via rename)."""
    import json
    import tempfile
    data = [{"pid": pid, "fmt": fmt, "w": w} for (pid, fmt), w in weights.items()]
    cache = {"ts": datetime.datetime.now(datetime.timezone.utc).timestamp(), "data": data}
    try:
        cache_dir = os.path.dirname(_WEIGHTS_CACHE_PATH)
        os.makedirs(cache_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp_path, _WEIGHTS_CACHE_PATH)  # atomique sur POSIX
        except BaseException:
            # Nettoyage du fichier temporaire en cas d'erreur
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:
        _log.warning("Impossible de sauvegarder le cache poids cartons", exc_info=True)


def fetch_carton_weights() -> dict[tuple[int, str], float]:
    """
    Récupère les poids cartons depuis EasyBeer pour tous les produits finis.
    Utilise un cache fichier de 24h pour éviter les appels API lents.

    1. POST /stock/produits → arbre des stocks (1 appel)
    2. GET /stock/produit/edition/{id} pour chaque stock (N appels)

    Retourne :
        {(idProduit, fmt_str): poidsUnitaire_kg, ...}
        ex: {(42514, "12x33"): 6.741, (42514, "6x75"): 7.23, ...}
    """
    # ── Cache ──
    cached = _load_weights_cache()
    if cached is not None:
        _log.debug("Cache poids cartons valide (%d entrees)", len(cached))
        return cached

    # ── Fetch depuis l'API ──
    _log.info("Fetch poids cartons depuis EasyBeer (cache expire ou absent)")
    import time

    payload = {"idBrasserie": int(os.environ.get("EASYBEER_ID_BRASSERIE", "0"))}
    r = requests.post(
        f"{BASE}/stock/produits",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "stock/produits")
    data = r.json()

    weights: dict[tuple[int, str], float] = {}
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

            try:
                detail = get_stock_produit_detail(sid)
                poids = float(detail.get("poidsUnitaire", 0) or 0)
                if poids > 0:
                    weights[(id_produit, fmt_str)] = poids
            except Exception:
                _log.warning("Erreur fetch detail stock %s", sid, exc_info=True)

            time.sleep(0.3)  # Rate-limit EasyBeer

    _log.info("Fetch poids cartons termine : %d poids recuperes", len(weights))
    _save_weights_cache(weights)
    return weights


# ─── Historique stock contenants ──────────────────────────────────────────────

def get_contenant_historique(
    *,
    date_debut: str | None = None,
    date_fin: str | None = None,
    ids_matieres_premieres: list[int] | None = None,
    type_mouvement: str | None = None,
    per_page: int = 200,
) -> list[dict[str, Any]]:
    """
    POST /stock/contenant/historique  (paginé)
    → Historique complet des mouvements de stock contenants / MP.

    Paramètres :
      date_debut / date_fin : bornes ISO (ex: "2026-01-01T00:00:00.000Z")
      ids_matieres_premieres : filtrer sur certaines MP (ex: [95553])
      type_mouvement : "RETRAIT" (sorties) | "AJOUT" (entrées) | None (tout)
      per_page : résultats par page (max 200)

    Retourne la liste complète (toutes pages) de ModeleStockContenantHistorique :
      {
        "stock": "Cartons 12x33cl SYMBIOSE",
        "date": 1740499731000,          ← timestamp ms
        "difference": -148,
        "quantiteAvant": 7870,
        "quantiteApres": 7722,
        "valorisationApres": 1776.06,
        "action": "Conditionnement",
        "batch": "Batch KDF16022026",
        "auteur": "Nicolas Pradignac",
        "commentaire": "",
        "numeroLot": "",
        "entrepot": "",
        "fournisseur": ""
      }
    """
    ep = "stock/contenant/historique"

    # Construire le filtre
    filtre: dict[str, Any] = {}

    if date_debut or date_fin:
        filtre["periode"] = {
            "dateDebut": date_debut or "2020-01-01T00:00:00.000Z",
            "dateFin": date_fin or datetime.datetime.now(
                datetime.timezone.utc
            ).strftime("%Y-%m-%dT23:59:59.999Z"),
            "type": "PERIODE_LIBRE",
        }

    if ids_matieres_premieres:
        filtre["idsMatieresPremieres"] = ids_matieres_premieres

    if type_mouvement:
        filtre["typeMouvement"] = type_mouvement  # "AJOUT" ou "RETRAIT"

    # Pagination
    all_items: list[dict[str, Any]] = []
    page = 0

    while True:
        r = requests.post(
            f"{BASE}/{ep}",
            params={
                "numeroPage": page,
                "nombreParPage": per_page,
                "colonneTri": "-date",
            },
            json=filtre,
            auth=_auth(),
            timeout=TIMEOUT,
        )
        _check_response(r, ep)
        data = r.json()

        items = data.get("liste") or []
        all_items.extend(items)

        total_pages = data.get("totalPages", 1)
        _log.debug(
            "contenant/historique page %d/%d — %d éléments",
            page + 1, total_pages, len(items),
        )

        page += 1
        if page >= total_pages or not items:
            break

    _log.info(
        "contenant/historique : %d mouvements récupérés (filtre MP=%s, période=%s→%s)",
        len(all_items),
        ids_matieres_premieres or "toutes",
        date_debut or "∞",
        date_fin or "maintenant",
    )
    return all_items


def get_clients(
    page: int = 0,
    per_page: int = 100,
    sort_by: str = "libelle",
    sort_mode: str = "ASC",
    filtre: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    POST /parametres/client/liste
    → Page de clients (paginée).

    Paramètres :
      page      : numéro de page (0-indexé)
      per_page  : résultats par page (max conseillé : 200)
      sort_by   : colonne de tri ("libelle", "id", ...)
      sort_mode : "ASC" | "DESC"
      filtre    : ModeleClientFiltre — critères optionnels, ex :
                  {"actif": True, "recherche": "dupont", "inclureProspect": False}

    Réponse : ListePagineeOfModeleClient
      {
        "liste": [
          {
            "id": 123,
            "libelle": "Nom Client",
            "email": "...",
            "telephone": "...",
            "codePostal": "...",
            "actif": true,
            ...
          }
        ],
        "totalElements": 250,
        "totalPages": 3
      }
    """
    r = requests.post(
        f"{BASE}/parametres/client/liste",
        params={
            "colonneTri":    sort_by,
            "mode":          sort_mode,
            "nombreParPage": per_page,
            "numeroPage":    page,
        },
        json=filtre or {},
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "client/liste")
    return r.json()


_MAX_PAGINATION_PAGES = 50  # garde-fou : jamais plus de 50 pages


def get_all_clients(
    sort_by: str = "libelle",
    sort_mode: str = "ASC",
    filtre: dict[str, Any] | None = None,
    per_page: int = 200,
) -> list[dict[str, Any]]:
    """
    Récupère TOUS les clients en gérant automatiquement la pagination.
    Limité à _MAX_PAGINATION_PAGES pages (garde-fou contre boucle infinie).

    Exemple :
      clients = get_all_clients(filtre={"actif": True})
      # → liste complète des clients actifs, toutes pages confondues
    """
    all_clients: list[dict[str, Any]] = []
    page = 0
    while page < _MAX_PAGINATION_PAGES:
        resp = get_clients(page=page, per_page=per_page, sort_by=sort_by,
                           sort_mode=sort_mode, filtre=filtre)
        liste = resp.get("liste") or []
        all_clients.extend(liste)
        total_pages = resp.get("totalPages", 1)
        page += 1
        if page >= total_pages or not liste:
            break
    else:
        _log.warning("get_all_clients : limite de %d pages atteinte", _MAX_PAGINATION_PAGES)
    return all_clients


# ─── Matériel (cuves, équipements) ────────────────────────────────────────────

def get_all_materiels() -> list[dict[str, Any]]:
    """
    GET /parametres/materiel/liste/all
    → Liste complète du matériel EasyBeer (non paginée).

    Champs utiles :
      - idMateriel, code, identifiant, volume
      - type.code  (CUVE_FABRICATION, CUVE_FERMENTATION, …)
      - etatCourant.code  (DISPONIBLE, AFFECTE, LAVAGE, MAINTENANCE)
    """
    r = requests.get(
        f"{BASE}/parametres/materiel/liste/all",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


# ─── Produits & Entrepôts ─────────────────────────────────────────────────────

def get_all_products() -> list[dict[str, Any]]:
    """
    GET /parametres/produit/liste/all
    → Liste complète des produits EasyBeer (non paginée).

    Champs utiles : idProduit, libelle
    """
    r = requests.get(
        f"{BASE}/parametres/produit/liste/all",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def get_warehouses() -> list[dict[str, Any]]:
    """
    GET /parametres/entrepot/liste
    → Liste de tous les entrepôts.

    Champs utiles : idEntrepot, libelle, nom, principal
    """
    r = requests.get(
        f"{BASE}/parametres/entrepot/liste",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def get_product_detail(id_produit: int) -> dict[str, Any]:
    """
    GET /parametres/produit/edition/{idProduit}
    → Détail complet d'un produit, incluant recettes et étapes.

    Champs utiles :
      - recettes[0].ingredients[]  → ingrédients avec quantités
      - recettes[0].volumeRecette  → volume de référence de la recette (litres)
      - etapes[]                   → étapes de production
    """
    r = requests.get(
        f"{BASE}/parametres/produit/edition/{id_produit}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


# ── Helpers calcul de volume avec aromatisation ──────────────────────────


def compute_aromatisation_volume(id_produit: int) -> tuple[float, float]:
    """
    Récupère la recette d'un produit et calcule le volume d'ingrédients
    ajoutés à l'étape d'aromatisation (jus, arômes).

    Retourne ``(A_R, R)`` :
      - ``A_R`` : volume total d'aromatisation à l'échelle de référence (litres,
        en considérant 1 kg = 1 L).
      - ``R``   : volume de référence de la recette (litres).

    Si la recette n'existe pas ou ne contient pas d'ingrédients
    d'aromatisation, ``A_R = 0``.
    """
    detail = get_product_detail(id_produit)
    recettes = detail.get("recettes") or []
    if not recettes:
        return 0.0, 0.0

    recette = recettes[0]
    R = float(recette.get("volumeRecette", 0) or 0)
    if R <= 0:
        return 0.0, 0.0

    A_R = 0.0
    for ing in recette.get("ingredients") or []:
        etape = ing.get("brassageEtape") or {}
        etape_name = (etape.get("nom") or etape.get("libelle") or "").lower()
        if "aromatisation" in etape_name:
            A_R += float(ing.get("quantite", 0) or 0)

    return A_R, R


def compute_v_start_max(
    capacity_L: float,
    transfer_loss_L: float,
    bottling_loss_L: float,
    A_R: float,
    R: float,
) -> tuple[float, float]:
    """
    Calcule le volume de départ max (V_start) et le volume embouteillé.

    Physique :
      1. Fermentation  : V_start litres dans la cuve
      2. Transfert      : −transfer_loss → V_start − Lt
      3. Aromatisation  : +A litres (A = A_R × V_start / R)
         → V_start − Lt + A  ≤  capacity  (contrainte de non-débordement)
      4. Embouteillage  : −bottling_loss → V_embouteillé

    Retourne ``(V_start_max, V_embouteillé)`` en litres.
    """
    C = capacity_L
    Lt = transfer_loss_L
    Lb = bottling_loss_L

    if R <= 0 or A_R <= 0:
        # Pas de recette ou pas d'aromatisation → comportement classique
        return C, max(C - Lt - Lb, 0.0)

    # V_start × (1 + A_R/R) ≤ C + Lt  →  V_start ≤ (C + Lt) × R / (R + A_R)
    v_max_formula = (C + Lt) * R / (R + A_R)
    V_start = min(C, v_max_formula)

    # Volume après aromatisation (= capacité cuve si V_start = v_max_formula)
    A_scaled = A_R * (V_start / R)
    V_bottled = V_start - Lt + A_scaled - Lb

    return V_start, max(V_bottled, 0.0)


def compute_dilution_ingredients(id_produit: int, V_start: float) -> dict[str, float]:
    """
    Récupère les ingrédients de l'étape de dilution / préparation sirop
    d'une recette EasyBeer, mis à l'échelle par rapport au volume de départ V_start.

    Noms d'étapes reconnus (case-insensitive) :
      - "Préparation sirop" / "Preparation sirop"
      - "Dilution"

    Retourne un dict {libelle_ingredient: quantite_kg}.
    """
    import unicodedata as _ud

    def _normalize(s: str) -> str:
        s = _ud.normalize("NFKD", s)
        s = "".join(ch for ch in s if not _ud.combining(ch))
        return s.lower()

    STEP_KEYWORDS = ("preparation sirop", "dilution")
    # Les grains de kéfir sont dans l'étape "Fermentation", pas "Préparation sirop"
    GRAIN_STEP_KEYWORDS = ("fermentation",)
    GRAIN_INGREDIENT_KEYWORDS = ("grain",)

    detail = get_product_detail(id_produit)
    recettes = detail.get("recettes") or []
    if not recettes:
        return {}

    recette = recettes[0]
    R = float(recette.get("volumeRecette", 0) or 0)
    if R <= 0:
        return {}

    ratio = V_start / R
    result: dict[str, float] = {}
    for ing in recette.get("ingredients") or []:
        etape = ing.get("brassageEtape") or {}
        etape_name = _normalize(etape.get("nom") or etape.get("libelle") or "")
        mp = ing.get("matierePremiere") or {}
        libelle = mp.get("libelle", "")
        if not libelle:
            libelle = f"Ingredient #{ing.get('ordre', '?')}"
        lib_norm = _normalize(libelle)

        # Ingrédients de l'étape Préparation sirop / Dilution
        if any(kw in etape_name for kw in STEP_KEYWORDS):
            qty = float(ing.get("quantite", 0) or 0) * ratio
            result[libelle] = round(qty, 2)
        # Grains de kéfir : étape Fermentation
        elif any(kw in etape_name for kw in GRAIN_STEP_KEYWORDS) and any(kw in lib_norm for kw in GRAIN_INGREDIENT_KEYWORDS):
            qty = float(ing.get("quantite", 0) or 0) * ratio
            result[libelle] = round(qty, 2)

    return result


def create_brassin(payload: dict[str, Any]) -> dict[str, Any]:
    """
    POST /brassin/enregistrer
    → Crée un nouveau brassin dans EasyBeer.

    Payload minimal (ModeleBrassin) :
      {
        "nom": "Brassin Gingembre — 2026-02-23",
        "volume": 5000.0,                              # litres
        "dateDebutFormulaire": "2026-02-23T00:00:00.000Z",
        "produit": {"idProduit": 123},
        "entrepot": {"idEntrepot": 1}
      }

    Retourne : {"id": <int>}  — l'ID du brassin créé.
    """
    r = requests.post(
        f"{BASE}/brassin/enregistrer",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "brassin/enregistrer")
    return r.json()


# ─── Brassins ─────────────────────────────────────────────────────────────────

def get_brassins_en_cours() -> list[dict[str, Any]]:
    """
    GET /brassin/en-cours/liste
    → Liste des brassins actuellement en cours de production.

    Chaque élément : ModeleBrassin (résumé)
      {
        "idBrassin": 456,
        "nom": "KGI23022026",
        "volume": 7200.0,
        "dateDebutFormulaire": "2026-02-23T07:30:00.000Z",
        "produit": {"idProduit": 123, "libelle": "Kéfir Gingembre", ...},
        "enCours": true,
        "termine": false,
        "annule": false,
        ...
      }
    """
    r = requests.get(
        f"{BASE}/brassin/en-cours/liste",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def get_brassins_archives(
    nombre: int = 3,
    jours: int = 60,
) -> list[dict[str, Any]]:
    """
    Retourne les *nombre* brassins les plus récents qui ne sont plus en cours.

    Stratégie :
      1. GET /brassin/en-cours/liste  → IDs des brassins en cours
      2. POST /brassin/liste (période = *jours* derniers jours) → tous les brassins
      3. Exclut les en cours, trie par dateDebutFormulaire desc, prend les N premiers

    Chaque élément : ModeleBrassin (même format que get_brassins_en_cours).
    """
    # 1. IDs des brassins en cours
    en_cours_ids: set[int] = set()
    try:
        for b in get_brassins_en_cours():
            bid = b.get("idBrassin")
            if bid:
                en_cours_ids.add(bid)
    except Exception:
        _log.warning("Erreur fetch brassins en cours pour archives", exc_info=True)

    # 2. Tous les brassins sur la fenetre
    now = datetime.datetime.now(datetime.timezone.utc)
    date_fin = now.strftime("%Y-%m-%dT23:59:59.999Z")
    date_debut = (now - datetime.timedelta(days=jours)).strftime("%Y-%m-%dT00:00:00.000Z")

    r = requests.post(
        f"{BASE}/brassin/liste",
        json={
            "dateDebut": date_debut,
            "dateFin": date_fin,
            "type": "PERIODE_LIBRE",
        },
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    all_brassins = data if isinstance(data, list) else []

    # 3. Exclure en cours + petits brassins (< 100L = garde grains, tests…)
    archived = [
        b for b in all_brassins
        if b.get("idBrassin") not in en_cours_ids
        and float(b.get("volume") or 0) >= 100
    ]

    # Tri par date desc -- extraite du nom (ex: KGI13022026 -> 13/02/2026)
    def _sort_key(b: dict) -> str:
        nom = b.get("nom") or ""
        m = re.search(r"(\d{8})$", nom)
        if m:
            ddmmyyyy = m.group(1)
            # Convertir DDMMYYYY → YYYYMMDD pour tri lexicographique
            return ddmmyyyy[4:8] + ddmmyyyy[2:4] + ddmmyyyy[0:2]
        # Fallback : dateDebutFormulaire (timestamp ms)
        raw = b.get("dateDebutFormulaire")
        if isinstance(raw, (int, float)):
            return str(int(raw))
        return "0"

    archived.sort(key=_sort_key, reverse=True)
    return archived[:nombre]


def get_brassin_detail(id_brassin: int) -> dict[str, Any]:
    """
    GET /brassin/{idBrassin}
    → Détail complet d'un brassin, incluant productions et planifications.

    Champs utiles :
      - productions[]                  → production réelle (après conditionnement)
        - produit.libelle, quantite, conditionnement, dateLimiteUtilisationOptimaleFormulaire
      - planificationsProductions[]    → production planifiée (avant conditionnement)
        - produit, quantite, conditionnement, dateLimiteUtilisationOptimale
      - produit.libelle                → nom du produit (ex: "Kéfir Gingembre")
      - volume                         → volume en litres
      - dateDebutFormulaire            → date de début ISO
      - dateConditionnementPrevue      → date d'embouteillage prévue
    """
    r = requests.get(
        f"{BASE}/brassin/{id_brassin}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


# ─── Planification de conditionnement ─────────────────────────────────────────

def get_planification_matrice(id_brassin: int, id_entrepot: int) -> dict[str, Any]:
    """
    GET /brassin/planification-conditionnement/matrice
    → Matrice des contenants × packagings pour un brassin et un entrepôt.

    Réponse : ModeleMatricePlanificationConditionnement
      {
        "contenants": [
          {
            "modeleContenant": {"idContenant": 1, "libelle": "Bouteille - 0.33L", ...},
            "productions": [...]
          }
        ],
        "packagings": [
          {"idLot": 5, "libelle": "Carton de 12", "quantite": 0, "visible": true}
        ],
        "produitsDerives": [...]
      }
    """
    r = requests.get(
        f"{BASE}/brassin/planification-conditionnement/matrice",
        params={"idBrassin": id_brassin, "idEntrepot": id_entrepot},
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def add_planification_conditionnement(payload: dict[str, Any]) -> Any:
    """
    POST /brassin/planification-conditionnement/ajouter
    → Ajoute une planification de conditionnement à un brassin.

    Payload : ModelePlanificationConditionnement
      {
        "idBrassin": 456,
        "idProduit": 123,
        "idEntrepot": 1,
        "date": "2026-03-02T23:00:00.000Z",
        "dateLimiteUtilisationOptimale": "2027-02-23T00:00:00.000Z",
        "numeroLot": "",
        "elements": [
          {"idContenant": 1, "idLot": 5, "quantite": 50}
        ]
      }
    """
    r = requests.post(
        f"{BASE}/brassin/planification-conditionnement/ajouter",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "planification-conditionnement/ajouter")
    try:
        return r.json()
    except Exception:
        return {"status": "ok"}


def get_code_barre_matrice() -> dict[str, Any]:
    """
    GET /parametres/code-barre/matrice
    → Matrice complète des codes-barres par produit.

    Réponse : ModeleMatriceCodeBarre
      {
        "produits": [
          {
            "modeleProduit": {"idProduit": 123, "libelle": "Kéfir Gingembre", ...},
            "codesBarres": [
              {
                "code": "3770014427014",
                "id": 456,
                "modeleContenant": {"idContenant": 1, "contenance": 0.33, ...},
                "modeleLot": {"idLot": 5, "libelle": "Carton de 12", ...},
                "modeleProduit": {"idProduit": 123, ...}
              }
            ]
          }
        ],
        "conditionnements": [...]
      }
    """
    r = requests.get(
        f"{BASE}/parametres/code-barre/matrice",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def upload_fichier_brassin(
    id_brassin: int,
    file_bytes: bytes,
    filename: str,
    commentaire: str = "",
) -> dict[str, Any]:
    """
    POST /brassin/upload/{idBrassin}
    → Upload un fichier (Excel, PDF…) dans l'onglet Fichiers du brassin.

    Paramètres :
      id_brassin  : ID du brassin cible
      file_bytes  : contenu du fichier en bytes
      filename    : nom du fichier (ex: "Fiche de production.xlsx")
      commentaire : commentaire optionnel

    Retourne : ModeleUpload  {id, nom, taille, mimeType, ...}
    """
    params: dict[str, str] = {}
    if commentaire:
        params["commentaire"] = commentaire

    ep = f"brassin/upload/{id_brassin}"
    _backoff = (5, 15, 30)  # délais entre retries (secondes)

    for attempt in range(len(_backoff) + 1):
        r = requests.post(
            f"{BASE}/{ep}",
            params=params,
            files={"fichier": (filename, file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            auth=_auth(),
            timeout=60,  # timeout plus long pour les uploads
        )
        # Rate-limit EasyBeer : 429 classique OU 400 avec ban temporaire
        _limited = (
            r.status_code == 429
            or (r.status_code == 400 and "banned" in (r.text or "")[:500].lower())
        )
        if not _limited or attempt >= len(_backoff):
            break
        delay = _backoff[attempt]
        _log.warning(
            "Upload %s : rate-limited HTTP %d (tentative %d/%d) — retry dans %ds",
            ep, r.status_code, attempt + 1, len(_backoff), delay,
        )
        _time.sleep(delay)

    _check_response(r, ep)
    try:
        return r.json()
    except Exception:
        return {"status": "ok"}
