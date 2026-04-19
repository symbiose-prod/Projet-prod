"""
common/easybeer/models.py
=========================
Typed dataclasses for EasyBeer API responses — stdlib only (no pydantic).

Each class exposes a ``from_dict()`` factory that parses defensively:
- missing / null fields → safe defaults (0, "", [])
- wrong type → safe default + warning log
- extra fields → ignored (forward-compat)

Usage (opt-in, not forced on callers yet):

    from common.easybeer.models import AutonomieResponse
    raw = get_autonomie_stocks(30)  # dict
    autonomie = AutonomieResponse.from_dict(raw)
    for p in autonomie.produits:
        print(p.libelle, p.autonomie)

Coexiste avec l'API ``dict[str, Any]`` existante — on migrera les callers
progressivement au fil des pages touchées.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger("ferment.easybeer.models")


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _as_str(v: Any, default: str = "") -> str:
    return str(v) if v is not None else default


@dataclass(frozen=True)
class AutonomieProduit:
    """Une ligne de la réponse ``/indicateur/autonomie-stocks``.

    Schema EasyBeer: ``ModeleAutonomie.produits[]``.

    Mapping des champs EasyBeer (source vérité → commentaire dans stocks.py) :
        volume            = Volume VENDU (hL) sur la fenêtre d'analyse
        volumeVirtuel     = Volume en STOCK (hL) courant
        quantite          = Cartons VENDUS sur la fenêtre d'analyse
        quantiteVirtuelle = Cartons en STOCK courant
        autonomie         = Jours de stock restants (= stock / conso journalière)
    """
    libelle: str
    autonomie: float                  # jours
    quantite_virtuelle: float         # cartons en stock
    quantite: float                   # cartons vendus (fenêtre)
    volume: float                     # hL vendus (fenêtre)
    volume_virtuel: float             # hL en stock

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AutonomieProduit:
        if not isinstance(d, dict):
            _log.warning("AutonomieProduit.from_dict: got %s", type(d).__name__)
            d = {}
        return cls(
            libelle=_as_str(d.get("libelle")),
            autonomie=_as_float(d.get("autonomie")),
            quantite_virtuelle=_as_float(d.get("quantiteVirtuelle")),
            quantite=_as_float(d.get("quantite")),
            volume=_as_float(d.get("volume")),
            volume_virtuel=_as_float(d.get("volumeVirtuel")),
        )


@dataclass(frozen=True)
class StockProduitFormat:
    """Une ligne ``stocksProduits[]`` d'un AutonomieProduit (un format d'un produit)."""
    libelle: str                # ex: "6x750 - Relais Vert"
    quantite: float             # cartons vendus (fenêtre)
    quantite_virtuelle: float   # cartons en stock
    volume: float               # hL vendus
    volume_virtuel: float       # hL en stock
    lot_quantite: int           # bouteilles par carton
    contenance: float           # litres par bouteille

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StockProduitFormat:
        if not isinstance(d, dict):
            return cls("", 0.0, 0.0, 0.0, 0.0, 0, 0.0)
        lot = d.get("lot") if isinstance(d.get("lot"), dict) else {}
        cont = d.get("contenant") if isinstance(d.get("contenant"), dict) else {}
        return cls(
            libelle=_as_str(d.get("libelle")),
            quantite=_as_float(d.get("quantite")),
            quantite_virtuelle=_as_float(d.get("quantiteVirtuelle")),
            volume=_as_float(d.get("volume")),
            volume_virtuel=_as_float(d.get("volumeVirtuel")),
            lot_quantite=_as_int(lot.get("quantite")),
            contenance=_as_float(cont.get("contenance")),
        )


@dataclass(frozen=True)
class AutonomieResponse:
    """Réponse complète ``/indicateur/autonomie-stocks``."""
    produits: list[AutonomieProduit] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AutonomieResponse:
        if not isinstance(d, dict):
            _log.warning("AutonomieResponse.from_dict: got %s", type(d).__name__)
            return cls()
        raw = d.get("produits")
        if raw is None:
            return cls()
        if not isinstance(raw, list):
            _log.warning(
                "AutonomieResponse: 'produits' attendu liste, reçu %s",
                type(raw).__name__,
            )
            return cls()
        return cls(produits=[AutonomieProduit.from_dict(p) for p in raw])


@dataclass(frozen=True)
class MatierePremiere:
    """Schema EasyBeer: ``ModeleMatierePremiere``."""
    id_matiere_premiere: int
    libelle: str
    quantite_virtuelle: float
    seuil_bas: float
    type_code: str  # ex: "CONDITIONNEMENT", "INGREDIENT"
    unite_symbole: str  # ex: "u", "kg"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MatierePremiere:
        if not isinstance(d, dict):
            return cls(0, "", 0.0, 0.0, "", "")
        t = d.get("type") if isinstance(d.get("type"), dict) else {}
        u = d.get("unite") if isinstance(d.get("unite"), dict) else {}
        return cls(
            id_matiere_premiere=_as_int(d.get("idMatierePremiere")),
            libelle=_as_str(d.get("libelle")),
            quantite_virtuelle=_as_float(d.get("quantiteVirtuelle")),
            seuil_bas=_as_float(d.get("seuilBas")),
            type_code=_as_str(t.get("code")),
            unite_symbole=_as_str(u.get("symbole")),
        )
