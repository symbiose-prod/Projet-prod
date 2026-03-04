"""
common/easybeer/products.py
===========================
Product, warehouse, and equipment endpoints.
"""
from __future__ import annotations

from typing import Any

import requests

from ._client import BASE, TIMEOUT, _auth, retry_api


@retry_api
def get_all_products() -> list[dict[str, Any]]:
    """GET /parametres/produit/liste/all → Liste complete des produits."""
    r = requests.get(
        f"{BASE}/parametres/produit/liste/all",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


@retry_api
def get_warehouses() -> list[dict[str, Any]]:
    """GET /parametres/entrepot/liste → Liste de tous les entrepots."""
    r = requests.get(
        f"{BASE}/parametres/entrepot/liste",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


@retry_api
def get_product_detail(id_produit: int) -> dict[str, Any]:
    """GET /parametres/produit/edition/{id} → Detail complet d'un produit."""
    r = requests.get(
        f"{BASE}/parametres/produit/edition/{id_produit}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


@retry_api
def get_all_materiels() -> list[dict[str, Any]]:
    """GET /parametres/materiel/liste/all → Liste complete du materiel."""
    r = requests.get(
        f"{BASE}/parametres/materiel/liste/all",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []
