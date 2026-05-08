"""
common/brassin_builder.py
==========================
Pure business logic for EasyBeer brassin creation — extracted from ui/_production_easybeer.py.

All functions are pure (no UI, no API calls, no side effects) and thus fully unit-testable.
"""
from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from zoneinfo import ZoneInfo

_PARIS = ZoneInfo("Europe/Paris")

_BRASSIN_DATE_RE = re.compile(r"(\d{8})$")


def generate_brassin_code(
    gout: str,
    semaine_du: str,
    product_label: str,
) -> str:
    """
    Génère le code brassin (ex: 'KOR04032026', 'IPG04032026').

    - Infusion → 'IP' + première lettre du goût + date
    - Kéfir   → 'K'  + 2 premières lettres du goût + date
    """
    date_obj = _dt.date.fromisoformat(semaine_du)
    date_str = date_obj.strftime("%d%m%Y")
    if "infusion" in product_label.lower():
        return "IP" + gout[:1].upper() + date_str
    return "K" + gout[:2].upper() + date_str


def extract_date_from_brassin_code(nom: str | None) -> _dt.date | None:
    """Extrait la date métier (DDMMYYYY) depuis le suffixe du code brassin.

    Inverse de `generate_brassin_code`. Retourne None si le nom ne se termine
    pas par 8 chiffres ou si la date est invalide.
    """
    if not nom:
        return None
    m = _BRASSIN_DATE_RE.search(str(nom).strip())
    if not m:
        return None
    s = m.group(1)
    try:
        return _dt.date(int(s[4:8]), int(s[2:4]), int(s[0:2]))
    except ValueError:
        return None


def _local_to_utc_iso(date_iso: str, hour: int, minute: int) -> str:
    """Convertit une date + heure locale (Europe/Paris) en timestamp UTC pour l'API."""
    local = _dt.datetime.fromisoformat(date_iso).replace(
        hour=hour, minute=minute, tzinfo=_PARIS,
    )
    utc = local.astimezone(_dt.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_brassin_payload(
    *,
    code: str,
    vol_l: float,
    perte_litres: float,
    semaine_du: str,
    date_embout_iso: str,
    id_produit: int,
    ingredients: list[dict] | None = None,
    planif_etapes: list[dict] | None = None,
) -> dict:
    """
    Construit le payload JSON pour POST /brassin.

    Fonction pure : prend des valeurs scalaires, retourne un dict.
    """
    payload: dict = {
        "nom": code,
        "volume": round(vol_l, 1),
        "pourcentagePerte": round(perte_litres / vol_l * 100, 2) if vol_l > 0 else 0,
        "dateDebutFormulaire": _local_to_utc_iso(semaine_du, 8, 30),
        "dateConditionnementPrevue": _local_to_utc_iso(date_embout_iso, 23, 0),
        "produit": {"idProduit": id_produit},
        "type": {"code": "LOCALE"},
        "deduireMatierePremiere": True,
        "changementEtapeAutomatique": True,
    }
    if ingredients:
        payload["ingredients"] = ingredients
    if planif_etapes:
        payload["planificationsEtapes"] = planif_etapes
    return payload


def _norm_etape(s: str) -> str:
    """Normalise le nom d'une étape : supprime les accents, lowercase."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower()


def scale_recipe_ingredients(
    recette: dict,
    vol_l: float,
) -> list[dict]:
    """
    Met à l'échelle les ingrédients d'une recette pour un volume donné.

    Retourne une liste de dicts prêts pour l'API (sans les lots — ils sont
    distribués séparément via BatchLotTracker).
    """
    vol_recette = recette.get("volumeRecette", 0)
    ratio = vol_l / vol_recette if vol_recette > 0.1 else 1.0
    result: list[dict] = []
    for ing in recette.get("ingredients") or []:
        result.append({
            "idProduitIngredient": ing.get("idProduitIngredient"),
            "matierePremiere": ing.get("matierePremiere"),
            "quantite": round(ing.get("quantite", 0) * ratio, 2),
            "ordre": ing.get("ordre", 0),
            "unite": ing.get("unite"),
            "brassageEtape": ing.get("brassageEtape"),
            "modeleNumerosLots": [],
        })
    return result


def build_etape_planification(
    etapes: list[dict],
    cuve_a_id: int | None = None,
    cuve_b_id: int | None = None,
    cuve_dilution_id: int | None = None,
) -> list[dict]:
    """
    Construit la liste planificationsEtapes avec affectation de cuves.

    Règles d'affectation :
    - Fermentation / Aromatisation / Filtration → cuve A
    - Transfert / Garde → cuve B
    - Préparation / Sirop → cuve dilution
    """
    result: list[dict] = []
    for et in etapes:
        etape_nom = _norm_etape((et.get("brassageEtape") or {}).get("nom", ""))
        mat: dict = {}
        if cuve_a_id and (
            "fermentation" in etape_nom
            or "aromatisation" in etape_nom
            or "filtration" in etape_nom
        ):
            mat = {"idMateriel": cuve_a_id}
        elif cuve_b_id and (
            "transfert" in etape_nom or "garde" in etape_nom
        ):
            mat = {"idMateriel": cuve_b_id}
        elif cuve_dilution_id and (
            "preparation" in etape_nom or "sirop" in etape_nom
        ):
            mat = {"idMateriel": cuve_dilution_id}

        result.append({
            "produitEtape": {
                "idProduitEtape": et.get("idProduitEtape"),
                "brassageEtape": et.get("brassageEtape"),
                "ordre": et.get("ordre"),
                "duree": et.get("duree"),
                "unite": et.get("unite"),
                "etapeTerminee": False,
                "etapeEnCours": False,
            },
            "materiel": mat,
        })
    return result


def parse_packaging_lookup(matrice: dict) -> dict[str, int]:
    """
    Extrait le mapping {label_packaging_lower: idLot} depuis la matrice de conditionnement.
    """
    lookup: dict[str, int] = {}
    for pk in matrice.get("packagings", []):
        lbl = (pk.get("libelle") or "").strip().lower()
        if lbl and pk.get("idLot") is not None:
            lookup[lbl] = pk["idLot"]
    return lookup


def parse_derive_map(matrice: dict) -> dict[str, int]:
    """
    Extrait le mapping {keyword: idProduit} des produits dérivés (NIKO, INTER, WATER).
    """
    derive: dict[str, int] = {}
    for d in matrice.get("produitsDerives", []):
        lbl = (d.get("libelle") or "").lower()
        pid = d.get("idProduit")
        if not pid:
            continue
        if "niko" in lbl:
            derive["niko"] = pid
        elif "inter" in lbl:
            derive["inter"] = pid
        elif "water" in lbl:
            derive["water"] = pid
    return derive


def match_contenant_id(
    stock_label: str,
    vol_btl: float | None,
    contenants_by_vol: dict[float, list[dict]],
) -> int | None:
    """
    Trouve l'idContenant correspondant à un format bouteille.

    Gère le cas multi-contenants (pack SAFT vs standard) via le nom du packaging.
    """
    import pandas as _pd  # local import to avoid heavy import at module level

    if vol_btl is None or _pd.isna(vol_btl):
        return None

    vol_key = round(float(vol_btl), 2)
    candidates = contenants_by_vol.get(vol_key, [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].get("idContenant")

    # Multi-candidates : chercher pack vs standard
    pkg_m = re.search(r"((?:carton|pack|caisse|colis)\s+de\s+\d+)", stock_label, re.IGNORECASE)
    pkg_name = pkg_m.group(1).strip().lower() if pkg_m else ""
    is_pack = "pack" in pkg_name

    for c in candidates:
        c_lbl = (c.get("libelleAvecContenance") or c.get("libelle") or "").lower()
        if is_pack and "saft" in c_lbl:
            return c.get("idContenant")
        if not is_pack and "saft" not in c_lbl:
            return c.get("idContenant")
    return candidates[0].get("idContenant")
