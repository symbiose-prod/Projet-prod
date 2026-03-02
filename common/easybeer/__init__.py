"""
common/easybeer — EasyBeer API client package.

Re-exports all public symbols so that existing imports keep working:
    from common.easybeer import is_configured, create_brassin, ...
"""
from __future__ import annotations

# --- client ---
from ._client import (
    BASE,
    TIMEOUT,
    is_configured,
    EasyBeerError,
)

# --- stocks ---
from .stocks import (
    get_autonomie_stocks_excel,
    get_autonomie_stocks,
    get_mp_lots,
    get_stock_produit_detail,
    fetch_carton_weights,
)

# --- history ---
from .history import get_contenant_historique

# --- clients ---
from .clients import get_clients, get_all_clients

# --- products ---
from .products import (
    get_all_products,
    get_warehouses,
    get_product_detail,
    get_all_materiels,
)

# --- recipes ---
from .recipes import (
    compute_aromatisation_volume,
    compute_v_start_max,
    compute_dilution_ingredients,
)

# --- brassins ---
from .brassins import (
    create_brassin,
    get_brassins_en_cours,
    get_brassins_archives,
    get_brassin_detail,
)

# --- conditioning ---
from .conditioning import (
    get_planification_matrice,
    add_planification_conditionnement,
    get_code_barre_matrice,
    upload_fichier_brassin,
)

__all__ = [
    # client
    "BASE", "TIMEOUT", "is_configured", "EasyBeerError",
    # stocks
    "get_autonomie_stocks_excel", "get_autonomie_stocks", "get_mp_lots",
    "get_stock_produit_detail", "fetch_carton_weights",
    # history
    "get_contenant_historique",
    # clients
    "get_clients", "get_all_clients",
    # products
    "get_all_products", "get_warehouses", "get_product_detail", "get_all_materiels",
    # recipes
    "compute_aromatisation_volume", "compute_v_start_max", "compute_dilution_ingredients",
    # brassins
    "create_brassin", "get_brassins_en_cours", "get_brassins_archives", "get_brassin_detail",
    # conditioning
    "get_planification_matrice", "add_planification_conditionnement",
    "get_code_barre_matrice", "upload_fichier_brassin",
]
