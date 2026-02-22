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
