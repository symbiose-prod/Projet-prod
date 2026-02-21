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
import os, json, sys
import requests
from datetime import datetime, timedelta

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
        except Exception:
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
    except Exception as e:
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
    except Exception as e:
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

print(f"\n{'='*60}")
print("  Exploration terminée.")
print("  Envoie les lignes ✅ à Nicolas pour configurer les endpoints.")
print(f"{'='*60}\n")
