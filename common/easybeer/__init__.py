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
    EasyBeerError,
    get_session,
    is_configured,
)

# --- brassins ---
from .brassins import (
    create_brassin,
    get_brassin_detail,
    get_brassins_archives,
    get_brassins_en_cours,
)

# --- clients ---
from .clients import get_all_clients, get_clients

# --- conditioning ---
from .conditioning import (
    add_planification_conditionnement,
    get_code_barre_matrice,
    get_planification_matrice,
    upload_fichier_brassin,
)

# --- history ---
from .history import get_contenant_historique

# --- products ---
from .products import (
    get_all_materiels,
    get_all_products,
    get_product_detail,
    get_warehouses,
)

# --- recipes ---
from .recipes import (
    compute_aromatisation_volume,
    compute_dilution_ingredients,
    compute_v_start_max,
)

# --- stocks ---
from .stocks import (
    fetch_carton_weights,
    get_all_matieres_premieres,
    get_autonomie_stocks,
    get_autonomie_stocks_excel,
    get_mp_lots,
    get_stock_produit_detail,
)

__all__ = [
    # client
    "BASE", "TIMEOUT", "is_configured", "EasyBeerError", "get_session",
    # stocks
    "get_autonomie_stocks_excel", "get_autonomie_stocks", "get_all_matieres_premieres",
    "get_mp_lots", "get_stock_produit_detail", "fetch_carton_weights",
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
