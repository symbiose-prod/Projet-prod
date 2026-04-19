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


# ─── Brassins ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BrassinLight:
    """Résumé d'un brassin tel que renvoyé par les listes
    (``/brassin/en-cours``, ``/brassin/archives``).

    Ne contient pas les détails (recettes, ingrédients, fiches) — pour ça,
    utiliser un appel sur ``/brassin/{id}`` et mapper vers ``BrassinDetail``
    quand ce modèle sera ajouté.

    Le flag ``is_archive`` n'est pas dans la réponse EasyBeer : il est posé
    localement par :func:`common.services.ramasse_service.load_active_brassins`
    pour différencier les archives affichées à côté des brassins en cours.

    Le champ ``raw`` conserve la réponse API brute pour les callers pas encore
    migrés vers le modèle typé (ex: :func:`common.ramasse.build_ramasse_lines`
    qui attend un dict). Permet une migration progressive — les appels au
    sens typé (``b.nom``, ``b.id_brassin``, ``b.is_archive``…) cohabitent avec
    le chemin legacy (``b.raw`` → dict EasyBeer complet). À retirer quand
    tous les consommateurs seront typés.
    """
    id_brassin: int
    nom: str
    volume: float                  # L
    annule: bool
    produit_libelle: str           # produit.libelle (ex: "Kéfir Original")
    id_produit: int                # produit.idProduit
    is_archive: bool = False       # flag local, pas dans l'API
    raw: dict = field(default_factory=dict)  # payload API brut (compat)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BrassinLight:
        if not isinstance(d, dict):
            return cls(0, "", 0.0, False, "", 0, False, {})
        produit = d.get("produit") if isinstance(d.get("produit"), dict) else {}
        return cls(
            id_brassin=_as_int(d.get("idBrassin")),
            nom=_as_str(d.get("nom")),
            volume=_as_float(d.get("volume")),
            annule=bool(d.get("annule") or False),
            produit_libelle=_as_str(produit.get("libelle")),
            id_produit=_as_int(produit.get("idProduit")),
            is_archive=bool(d.get("_is_archive") or False),
            raw=d,
        )


# ─── Fournisseurs ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FournisseurContact:
    """Contact d'un fournisseur (une personne avec nom + email)."""
    nom: str
    prenom: str
    email: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FournisseurContact:
        if not isinstance(d, dict):
            return cls("", "", "")
        return cls(
            nom=_as_str(d.get("nom")),
            prenom=_as_str(d.get("prenom")),
            email=_as_str(d.get("email")),
        )

    @property
    def display_name(self) -> str:
        """Nom complet formaté pour affichage, ex: 'Jean Dupont'."""
        parts = [p for p in (self.prenom, self.nom) if p]
        return " ".join(parts)


@dataclass(frozen=True)
class Fournisseur:
    """Fiche fournisseur telle que renvoyée par ``/fournisseur/{id}``.

    Schema EasyBeer: ``ModeleFournisseur`` (simplifié — on n'expose que les
    champs réellement utilisés par Ferment Station : identité, contacts,
    adresse pour les commandes).
    """
    id_fournisseur: int
    nom: str
    email: str                      # email principal (fallback sur 1er contact)
    contacts: list[FournisseurContact]
    adresse_lignes: list[str]       # reformatée pour affichage (multi-lignes)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Fournisseur:
        if not isinstance(d, dict):
            return cls(0, "", "", [], [])

        # Contacts : liste de ContactFournisseur
        raw_contacts = d.get("contacts") or []
        contacts: list[FournisseurContact] = []
        if isinstance(raw_contacts, list):
            for c in raw_contacts:
                contacts.append(FournisseurContact.from_dict(c))

        # Email principal : champ direct du fournisseur, fallback 1er contact
        email = _as_str(d.get("email"))
        if not email and contacts:
            for c in contacts:
                if c.email:
                    email = c.email
                    break

        # Adresse : EasyBeer renvoie soit un dict {adresse, codePostal, ville, pays}
        # soit rien. On aplatit en liste de lignes non-vides.
        adr_obj = d.get("adresse") if isinstance(d.get("adresse"), dict) else {}
        lignes: list[str] = []
        rue = _as_str(adr_obj.get("adresse") or d.get("adresse") if not isinstance(d.get("adresse"), dict) else "")
        if isinstance(adr_obj, dict) and adr_obj:
            rue = _as_str(adr_obj.get("adresse"))
        if rue:
            lignes.append(rue)
        cp_ville_parts = []
        cp = _as_str(adr_obj.get("codePostal") if isinstance(adr_obj, dict) else "")
        ville = _as_str(adr_obj.get("ville") if isinstance(adr_obj, dict) else "")
        if cp:
            cp_ville_parts.append(cp)
        if ville:
            cp_ville_parts.append(ville)
        if cp_ville_parts:
            lignes.append(" ".join(cp_ville_parts))
        pays = _as_str(adr_obj.get("pays") if isinstance(adr_obj, dict) else "")
        if pays and pays.lower() not in ("france", "fr"):
            lignes.append(pays)

        return cls(
            id_fournisseur=_as_int(d.get("idFournisseur")),
            nom=_as_str(d.get("nom")),
            email=email,
            contacts=contacts,
            adresse_lignes=lignes,
        )
