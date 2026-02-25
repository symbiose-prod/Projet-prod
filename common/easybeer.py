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
  POST /parametres/client/liste                   → liste paginée des clients
  GET  /parametres/produit/liste/all              → tous les produits
  GET  /parametres/entrepot/liste                 → tous les entrepôts
  POST /brassin/enregistrer                       → créer un brassin
  GET  /brassin/en-cours/liste                    → brassins en cours
  GET  /brassin/{idBrassin}                       → détail complet d'un brassin
"""
from __future__ import annotations

import datetime
import os
from typing import Any

import requests

# ─── Config (variables d'environnement) ────────────────────────────────────────
EB_USER         = os.environ.get("EASYBEER_API_USER", "")
EB_PASS         = os.environ.get("EASYBEER_API_PASS", "")
EB_ID_BRASSERIE = int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013"))
BASE            = "https://api.easybeer.fr"
TIMEOUT         = 30  # secondes


def is_configured() -> bool:
    """True si les credentials Easy Beer sont présents."""
    return bool(EB_USER and EB_PASS)


def _auth() -> tuple[str, str]:
    return (EB_USER, EB_PASS)


def _dates(window_days: int) -> tuple[str, str]:
    """Retourne (date_debut_iso, date_fin_iso) pour une fenêtre de N jours jusqu'à aujourd'hui."""
    fin   = datetime.datetime.utcnow()
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
        "idBrasserie": EB_ID_BRASSERIE,
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
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} — {r.text[:500]}")
    return r.json()


def get_mp_all(status: str = "actif") -> list[dict[str, Any]]:
    """
    GET /stock/matieres-premieres/all
    → Liste de TOUTES les matières premières (ingrédients + conditionnements + divers).

    Chaque élément : ModeleMatierePremiere
      {
        "idMatierePremiere": 42,
        "libelle": "Carton 12×33cl",
        "quantite": 1200.0,           ← stock physique
        "quantiteVirtuelle": 1200.0,  ← stock virtuel
        "seuilBas": 500.0,
        "seuilHaut": 2000.0,
        "type": {"code": "CONDITIONNEMENT", "libelle": "...", "icone": "...", "uri": "..."},
        "unite": {"idUnite": 1, "nom": "unité", "symbole": "u", "coefficient": 1.0},
        "actif": true
      }

    Paramètre status : "actif" | "inactif" | "all"
    """
    r = requests.get(
        f"{BASE}/stock/matieres-premieres/all",
        params={"status": status},
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
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


def fetch_carton_weights() -> dict[tuple[int, str], float]:
    """
    Récupère les poids cartons depuis EasyBeer pour tous les produits finis.

    1. POST /stock/produits → arbre des stocks (1 appel)
    2. GET /stock/produit/edition/{id} pour chaque stock (N appels, 0.3s entre chaque)

    Retourne :
        {(idProduit, fmt_str): poidsUnitaire_kg, ...}
        ex: {(42514, "12x33"): 6.741, (42514, "6x75"): 7.23, ...}
    """
    import time

    payload = {"idBrasserie": int(os.environ.get("EASYBEER_ID_BRASSERIE", "0"))}
    r = requests.post(
        f"{BASE}/stock/produits",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} — {r.text[:500]}")
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
                pass

            time.sleep(1.0)  # Respecter le rate-limit EasyBeer (10 req/s max)

    return weights


def get_synthese_consommations_mp(window_days: int) -> dict[str, Any]:
    """
    POST /indicateur/synthese-consommations-mp
    → Synthèse des consommations de matières premières sur la période.

    Réponse : ModeleSyntheseConsoMP
      {
        "codeRetour": "OK",
        "syntheseConditionnement": {          ← PACKAGING (cartons, capsules, étiquettes)
          "cout": 1234.56,
          "quantite": 5000,
          "elements": [                       ← ModeleSyntheseConsoMPElement[]
            {
              "libelle": "Carton 12×33cl",
              "quantite": 1500.0,             ← qty consommée sur la période
              "unite": "carton",
              "idMatierePremiere": 42,
              "cout": 750.0
            }
          ]
        },
        "syntheseContenant": {...},           ← bouteilles vides
        "syntheseIngredient": {...},          ← levures, houblon, etc.
        "syntheseDivers": {...}
      }
    """
    r = requests.post(
        f"{BASE}/indicateur/synthese-consommations-mp",
        params={"forceRefresh": False},
        json=_indicator_payload(window_days),
        auth=_auth(),
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} — {r.text[:500]}")
    return r.json()


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
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} — {r.text[:500]}")
    return r.json()


def get_all_clients(
    sort_by: str = "libelle",
    sort_mode: str = "ASC",
    filtre: dict[str, Any] | None = None,
    per_page: int = 200,
) -> list[dict[str, Any]]:
    """
    Récupère TOUS les clients en gérant automatiquement la pagination.

    Exemple :
      clients = get_all_clients(filtre={"actif": True})
      # → liste complète des clients actifs, toutes pages confondues
    """
    all_clients: list[dict[str, Any]] = []
    page = 0
    while True:
        resp = get_clients(page=page, per_page=per_page, sort_by=sort_by,
                           sort_mode=sort_mode, filtre=filtre)
        liste = resp.get("liste") or []
        all_clients.extend(liste)
        total_pages = resp.get("totalPages", 1)
        page += 1
        if page >= total_pages or not liste:
            break
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
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} — {r.text[:500]}")
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
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} — {r.text[:500]}")
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

    r = requests.post(
        f"{BASE}/brassin/upload/{id_brassin}",
        params=params,
        files={"fichier": (filename, file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        auth=_auth(),
        timeout=TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} — {r.text[:500]}")
    try:
        return r.json()
    except Exception:
        return {"status": "ok"}
