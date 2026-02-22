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


def _excel_payload(window_days: int) -> dict[str, Any]:
    """Payload pour les endpoints /export/excel (utilisent l'objet 'periode')."""
    debut, fin = _dates(window_days)
    return {
        "idBrasserie": EB_ID_BRASSERIE,
        "periode": {"dateDebut": debut, "dateFin": fin},
    }


def _indicator_payload(window_days: int) -> dict[str, Any]:
    """
    Payload pour les endpoints JSON /indicateur/* (spec OpenAPI).
    Ces endpoints n'acceptent PAS l'objet 'periode' — ils utilisent
    dateCreationClientApres / dateCreationClientAvant comme filtre de période.
    """
    debut, fin = _dates(window_days)
    return {
        "idBrasserie":              EB_ID_BRASSERIE,
        "dateCreationClientApres":  debut,
        "dateCreationClientAvant":  fin,
        "deduireConditionnements":  False,
        "deduireDroitsAccise":      False,
        "deduireFraisLivraison":    False,
    }


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
        json=_indicator_payload(window_days),
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
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
        json=_indicator_payload(window_days),
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()
