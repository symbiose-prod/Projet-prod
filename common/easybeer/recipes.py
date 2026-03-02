"""
common/easybeer/recipes.py
==========================
Recipe-based calculations: aromatisation, V_start, dilution ingredients.
"""
from __future__ import annotations

from .products import get_product_detail


def compute_aromatisation_volume(id_produit: int) -> tuple[float, float]:
    """
    Calcule le volume d'aromatisation depuis la recette EasyBeer.
    Retourne (A_R, R) : volume aromatisation et volume reference.
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
    Calcule le volume de depart max (V_start) et le volume embouteille.
    Retourne (V_start_max, V_embouteille) en litres.
    """
    C = capacity_L
    Lt = transfer_loss_L
    Lb = bottling_loss_L

    if R <= 0 or A_R <= 0:
        return C, max(C - Lt - Lb, 0.0)

    v_max_formula = (C + Lt) * R / (R + A_R)
    V_start = min(C, v_max_formula)

    A_scaled = A_R * (V_start / R)
    V_bottled = V_start - Lt + A_scaled - Lb

    return V_start, max(V_bottled, 0.0)


def compute_dilution_ingredients(id_produit: int, V_start: float) -> dict[str, float]:
    """
    Recupere les ingredients de l'etape de dilution / preparation sirop,
    mis a l'echelle par rapport au volume de depart V_start.
    """
    import unicodedata as _ud

    def _normalize(s: str) -> str:
        s = _ud.normalize("NFKD", s)
        s = "".join(ch for ch in s if not _ud.combining(ch))
        return s.lower()

    STEP_KEYWORDS = ("preparation sirop", "dilution")
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

        if any(kw in etape_name for kw in STEP_KEYWORDS):
            qty = float(ing.get("quantite", 0) or 0) * ratio
            result[libelle] = round(qty, 2)
        elif any(kw in etape_name for kw in GRAIN_STEP_KEYWORDS) and any(
            kw in lib_norm for kw in GRAIN_INGREDIENT_KEYWORDS
        ):
            qty = float(ing.get("quantite", 0) or 0) * ratio
            result[libelle] = round(qty, 2)

    return result
