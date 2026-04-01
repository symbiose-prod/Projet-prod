#!/usr/bin/env python3
"""
explore_easybeer_api.py
=======================
Script d'exploration de l'API Easy Beer — à lancer sur le VPS.

Usage:
    cd /home/ubuntu/app
    source .env  # ou: export $(cat .env | xargs)
    python scripts/explore_easybeer_api.py

Résultat : affiche tous les endpoints qui répondent avec le statut + aperçu JSON.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

import requests

USER         = os.environ.get("EASYBEER_API_USER", "")
PASS         = os.environ.get("EASYBEER_API_PASS", "")
ID_BRASSERIE = int(os.environ.get("EASYBEER_ID_BRASSERIE", "2013"))
BASE         = "https://api.easybeer.fr"
AUTH         = (USER, PASS)

if not USER or not PASS:
    print("❌ EASYBEER_API_USER / EASYBEER_API_PASS non définis.")
    sys.exit(1)

fin    = datetime.utcnow()
debut  = fin - timedelta(days=30)
PERIODE = {
    "dateDebut": debut.strftime("%Y-%m-%dT00:00:00.000Z"),
    "dateFin":   fin.strftime("%Y-%m-%dT23:59:59.999Z"),
}

def _preview(r: requests.Response, max_chars: int = 800) -> str:
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        try:
            return json.dumps(r.json(), indent=2, ensure_ascii=False)[:max_chars]
        except (ValueError, TypeError):
            pass
    if "text" in ct or "html" in ct:
        return r.text[:max_chars]
    return f"(binaire {len(r.content)} octets)"

def GET(path: str) -> requests.Response | None:
    url = BASE + path
    try:
        r = requests.get(url, auth=AUTH, timeout=10)
        mark = "✅" if r.status_code == 200 else ("⚠️" if r.status_code < 500 else "❌")
        print(f"{mark} GET  {path}  →  {r.status_code}")
        if r.status_code == 200:
            print(_preview(r))
            print()
        return r
    except requests.RequestException as e:
        print(f"❌ GET  {path}  →  ERREUR: {e}")
        return None

def POST(path: str, payload: dict) -> requests.Response | None:
    url = BASE + path
    try:
        r = requests.post(url, json=payload, auth=AUTH, timeout=10)
        mark = "✅" if r.status_code == 200 else ("⚠️" if r.status_code < 500 else "❌")
        print(f"{mark} POST {path}  →  {r.status_code}")
        if r.status_code == 200:
            print(_preview(r))
            print()
        return r
    except requests.RequestException as e:
        print(f"❌ POST {path}  →  ERREUR: {e}")
        return None

BASE_PAYLOAD = {"idBrasserie": ID_BRASSERIE, "periode": PERIODE}

print(f"\n{'='*60}")
print(f"  Easy Beer API Explorer — brasserie {ID_BRASSERIE}")
print(f"  {BASE}")
print(f"{'='*60}\n")

# ─── Documentation / Swagger ─────────────────────────────────
print("── Documentation ──────────────────────────────────────")
GET("/swagger-ui.html")
GET("/v2/api-docs")
GET("/v3/api-docs")
GET("/openapi.json")
GET("/api-docs")

# ─── Indicateurs (on connaît autonomie-stocks) ───────────────
print("\n── Indicateurs ─────────────────────────────────────────")
GET(f"/indicateur?idBrasserie={ID_BRASSERIE}")
POST("/indicateur/stocks/export/excel", BASE_PAYLOAD)
POST("/indicateur/stock-articles/export/excel", BASE_PAYLOAD)
POST("/indicateur/stock-conditionnements/export/excel", BASE_PAYLOAD)
POST("/indicateur/consommation-articles/export/excel", BASE_PAYLOAD)
POST("/indicateur/mouvements-stocks/export/excel", BASE_PAYLOAD)

# ─── Stocks articles / conditionnements ──────────────────────
print("\n── Stocks ──────────────────────────────────────────────")
GET(f"/stock?idBrasserie={ID_BRASSERIE}")
GET(f"/stocks?idBrasserie={ID_BRASSERIE}")
GET(f"/stock/articles?idBrasserie={ID_BRASSERIE}")
GET(f"/stock/conditionnements?idBrasserie={ID_BRASSERIE}")
POST("/stock/export/excel", BASE_PAYLOAD)
POST("/stock/articles/export/excel", BASE_PAYLOAD)

# ─── Articles / Conditionnements ─────────────────────────────
print("\n── Articles / Conditionnements ─────────────────────────")
GET(f"/article?idBrasserie={ID_BRASSERIE}")
GET(f"/articles?idBrasserie={ID_BRASSERIE}")
GET(f"/conditionnement?idBrasserie={ID_BRASSERIE}")
GET(f"/conditionnements?idBrasserie={ID_BRASSERIE}")
GET(f"/article/conditionnement?idBrasserie={ID_BRASSERIE}")

# ─── Recettes / Nomenclature / BOM ───────────────────────────
print("\n── Recettes / Nomenclature ─────────────────────────────")
GET(f"/recette?idBrasserie={ID_BRASSERIE}")
GET(f"/recettes?idBrasserie={ID_BRASSERIE}")
GET(f"/nomenclature?idBrasserie={ID_BRASSERIE}")
GET(f"/bom?idBrasserie={ID_BRASSERIE}")
GET(f"/recette/liste?idBrasserie={ID_BRASSERIE}")
POST("/recette/export/excel", BASE_PAYLOAD)

# ─── Produits ────────────────────────────────────────────────
print("\n── Produits ────────────────────────────────────────────")
GET(f"/produit?idBrasserie={ID_BRASSERIE}")
GET(f"/produits?idBrasserie={ID_BRASSERIE}")
GET(f"/produit/liste?idBrasserie={ID_BRASSERIE}")
POST("/produit/export/excel", BASE_PAYLOAD)

# ─── Webhooks ───────────────────────────────────────────────
print("\n── Webhooks ────────────────────────────────────────────")
# Lister les webhooks existants
GET("/parametres/webhook/liste")

# Chercher les types de webhooks disponibles dans les référentiels
GET("/referentiel/webhook/type")
GET("/referentiel/webhook/types")
GET("/parametres/webhook/types")
GET("/parametres/webhook/type")
GET("/referentiel/type-webhook")

# Tenter d'enregistrer un webhook de test pour voir les types acceptés
# (avec un payload minimal pour voir l'erreur / les types attendus)
print("\n── Test enregistrement webhook (payload minimal) ──────")
POST("/parametres/webhook/enregistrer", {
    "url": "https://example.com/webhook-test",
    "libelle": "Test exploration",
})
# Tenter avec un type vide pour voir l'erreur
POST("/parametres/webhook/enregistrer", {
    "url": "https://example.com/webhook-test",
    "libelle": "Test exploration",
    "type": {"code": "", "libelle": ""},
})
# Types courants dans les ERP brassicoles
for code in [
    "BRASSIN", "STOCK", "COMMANDE", "CONDITIONNEMENT",
    "PRODUIT", "MATIERE_PREMIERE", "VENTE", "FACTURE",
    "ALL", "MOUVEMENT_STOCK", "LIVRAISON",
]:
    print(f"  → Test type '{code}'...")
    r = POST("/parametres/webhook/enregistrer", {
        "url": "https://example.com/webhook-test",
        "libelle": f"Test {code}",
        "type": {"code": code},
    })
    if r and r.status_code == 200:
        print(f"  ✅ Type '{code}' ACCEPTÉ !")
        # Supprimer immédiatement le webhook de test
        try:
            data = r.json()
            wh_id = data.get("idWebhook") or data.get("id")
            if wh_id:
                GET(f"/parametres/webhook/supprimer/{wh_id}")
                print(f"  🗑️  Webhook test {wh_id} supprimé")
        except Exception:
            pass

# Lister à nouveau pour voir ce qui a été créé
print("\n── Webhooks après tests ────────────────────────────────")
GET("/parametres/webhook/liste")

print(f"\n{'='*60}")
print("  Exploration terminée.")
print(f"{'='*60}\n")
