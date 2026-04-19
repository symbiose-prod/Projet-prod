"""Tests for common.easybeer.models — defensive dataclass parsing."""
from __future__ import annotations

from common.easybeer.models import (
    AutonomieProduit,
    AutonomieResponse,
    BrassinLight,
    Fournisseur,
    FournisseurContact,
    MatierePremiere,
    StockProduitFormat,
)


class TestAutonomieProduit:
    def test_full_dict(self):
        p = AutonomieProduit.from_dict({
            "libelle": "Kéfir Original",
            "autonomie": 28.5,
            "quantiteVirtuelle": 1150,
            "quantite": 500,
            "volume": 4.0,
            "volumeVirtuel": 12.0,
        })
        assert p.libelle == "Kéfir Original"
        assert p.autonomie == 28.5
        assert p.quantite_virtuelle == 1150.0
        assert p.quantite == 500.0
        assert p.volume == 4.0
        assert p.volume_virtuel == 12.0

    def test_missing_fields_default_to_zero(self):
        p = AutonomieProduit.from_dict({"libelle": "Kéfir"})
        assert p.libelle == "Kéfir"
        assert p.autonomie == 0.0
        assert p.quantite_virtuelle == 0.0
        assert p.quantite == 0.0
        assert p.volume == 0.0
        assert p.volume_virtuel == 0.0

    def test_null_fields_default_to_zero(self):
        p = AutonomieProduit.from_dict({
            "libelle": None,
            "autonomie": None,
            "quantiteVirtuelle": None,
            "quantite": None,
            "volume": None,
            "volumeVirtuel": None,
        })
        assert p.libelle == ""
        assert p.autonomie == 0.0
        assert p.quantite == 0.0
        assert p.volume_virtuel == 0.0

    def test_wrong_types_default_safely(self):
        p = AutonomieProduit.from_dict({
            "libelle": 42,  # int instead of str
            "autonomie": "abc",  # non-numeric string
            "quantiteVirtuelle": [1, 2],  # list
            "volume": {"nope": 1},  # dict
        })
        assert p.libelle == "42"  # str coercion works
        assert p.autonomie == 0.0
        assert p.quantite_virtuelle == 0.0
        assert p.volume == 0.0

    def test_non_dict_input(self):
        p = AutonomieProduit.from_dict(None)  # type: ignore
        assert p.libelle == ""
        assert p.autonomie == 0.0


class TestAutonomieResponse:
    def test_full_response(self):
        resp = AutonomieResponse.from_dict({
            "produits": [
                {"libelle": "A", "autonomie": 10, "quantiteVirtuelle": 100, "volume": 1.0},
                {"libelle": "B", "autonomie": 20, "quantiteVirtuelle": 200, "volume": 2.0},
            ]
        })
        assert len(resp.produits) == 2
        assert resp.produits[0].libelle == "A"
        assert resp.produits[1].autonomie == 20.0

    def test_null_produits_returns_empty(self):
        """EasyBeer sometimes returns {"produits": null} — must not crash."""
        resp = AutonomieResponse.from_dict({"produits": None})
        assert resp.produits == []

    def test_missing_produits_returns_empty(self):
        resp = AutonomieResponse.from_dict({})
        assert resp.produits == []

    def test_wrong_type_produits_returns_empty(self):
        resp = AutonomieResponse.from_dict({"produits": "not a list"})
        assert resp.produits == []

    def test_non_dict_input(self):
        resp = AutonomieResponse.from_dict(None)  # type: ignore
        assert resp.produits == []


class TestStockProduitFormat:
    def test_full_dict(self):
        f = StockProduitFormat.from_dict({
            "libelle": "6x750 - Relais Vert",
            "quantite": 500,
            "quantiteVirtuelle": 120,
            "volume": 22.5,
            "volumeVirtuel": 5.4,
            "lot": {"quantite": 6},
            "contenant": {"contenance": 0.75},
        })
        assert f.libelle == "6x750 - Relais Vert"
        assert f.quantite == 500.0
        assert f.quantite_virtuelle == 120.0
        assert f.volume == 22.5
        assert f.volume_virtuel == 5.4
        assert f.lot_quantite == 6
        assert f.contenance == 0.75

    def test_missing_lot_and_contenant(self):
        """lot/contenant absents — ne doit pas crasher."""
        f = StockProduitFormat.from_dict({
            "libelle": "X",
            "quantite": 1,
        })
        assert f.lot_quantite == 0
        assert f.contenance == 0.0

    def test_null_lot(self):
        f = StockProduitFormat.from_dict({
            "libelle": "X",
            "lot": None,
            "contenant": None,
        })
        assert f.lot_quantite == 0
        assert f.contenance == 0.0

    def test_non_dict_input(self):
        f = StockProduitFormat.from_dict(None)  # type: ignore
        assert f.libelle == ""
        assert f.quantite == 0.0


class TestMatierePremiere:
    def test_full_dict(self):
        mp = MatierePremiere.from_dict({
            "idMatierePremiere": 42,
            "libelle": "Carton 12×33cl",
            "quantiteVirtuelle": 1200.0,
            "seuilBas": 500.0,
            "type": {"code": "CONDITIONNEMENT"},
            "unite": {"symbole": "u"},
        })
        assert mp.id_matiere_premiere == 42
        assert mp.libelle == "Carton 12×33cl"
        assert mp.type_code == "CONDITIONNEMENT"
        assert mp.unite_symbole == "u"

    def test_missing_nested_objects(self):
        """type and unite absent — should not crash, default to empty."""
        mp = MatierePremiere.from_dict({
            "idMatierePremiere": 1,
            "libelle": "X",
            "quantiteVirtuelle": 0,
            "seuilBas": 0,
        })
        assert mp.type_code == ""
        assert mp.unite_symbole == ""

    def test_null_nested_objects(self):
        """type=null and unite=null — must not crash."""
        mp = MatierePremiere.from_dict({
            "idMatierePremiere": 1,
            "libelle": "X",
            "quantiteVirtuelle": 0,
            "seuilBas": 0,
            "type": None,
            "unite": None,
        })
        assert mp.type_code == ""
        assert mp.unite_symbole == ""


class TestBrassinLight:
    def test_full_dict(self):
        b = BrassinLight.from_dict({
            "idBrassin": 12345,
            "nom": "B-2026-042",
            "volume": 7200.0,
            "annule": False,
            "produit": {
                "idProduit": 77,
                "libelle": "Kéfir Original",
            },
        })
        assert b.id_brassin == 12345
        assert b.nom == "B-2026-042"
        assert b.volume == 7200.0
        assert b.annule is False
        assert b.produit_libelle == "Kéfir Original"
        assert b.id_produit == 77
        assert b.is_archive is False

    def test_archive_flag_from_local_hint(self):
        """_is_archive est un flag posé localement par load_active_brassins."""
        b = BrassinLight.from_dict({
            "idBrassin": 9,
            "nom": "B-old",
            "_is_archive": True,
        })
        assert b.is_archive is True

    def test_missing_produit_object(self):
        """produit absent ou null — pas de crash."""
        b = BrassinLight.from_dict({"idBrassin": 1, "nom": "X", "produit": None})
        assert b.produit_libelle == ""
        assert b.id_produit == 0

    def test_cancelled(self):
        b = BrassinLight.from_dict({"idBrassin": 1, "annule": True})
        assert b.annule is True

    def test_non_dict_input(self):
        b = BrassinLight.from_dict(None)  # type: ignore
        assert b.id_brassin == 0
        assert b.nom == ""


class TestFournisseurContact:
    def test_full_dict(self):
        c = FournisseurContact.from_dict({
            "nom": "Dupont",
            "prenom": "Jean",
            "email": "jean.dupont@example.com",
        })
        assert c.nom == "Dupont"
        assert c.prenom == "Jean"
        assert c.email == "jean.dupont@example.com"
        assert c.display_name == "Jean Dupont"

    def test_display_name_with_missing_parts(self):
        c1 = FournisseurContact.from_dict({"prenom": "Jean"})
        assert c1.display_name == "Jean"
        c2 = FournisseurContact.from_dict({"nom": "Dupont"})
        assert c2.display_name == "Dupont"
        c3 = FournisseurContact.from_dict({})
        assert c3.display_name == ""

    def test_non_dict_input(self):
        c = FournisseurContact.from_dict(None)  # type: ignore
        assert c.email == ""


class TestFournisseur:
    def test_full_dict(self):
        f = Fournisseur.from_dict({
            "idFournisseur": 42,
            "nom": "Verallia",
            "email": "commercial@verallia.com",
            "contacts": [
                {"nom": "Dupont", "prenom": "Jean", "email": "jean@verallia.com"},
                {"nom": "Martin", "prenom": "Marie", "email": "marie@verallia.com"},
            ],
            "adresse": {
                "adresse": "31 place des Corolles",
                "codePostal": "92400",
                "ville": "Courbevoie",
                "pays": "France",
            },
        })
        assert f.id_fournisseur == 42
        assert f.nom == "Verallia"
        assert f.email == "commercial@verallia.com"
        assert len(f.contacts) == 2
        assert f.contacts[0].display_name == "Jean Dupont"
        # Adresse : France filtrée (défaut FR)
        assert f.adresse_lignes == [
            "31 place des Corolles",
            "92400 Courbevoie",
        ]

    def test_email_fallback_to_first_contact(self):
        """Si pas d'email principal, on utilise celui du 1er contact qui en a un."""
        f = Fournisseur.from_dict({
            "idFournisseur": 1,
            "nom": "X",
            "contacts": [
                {"nom": "A", "email": ""},  # vide
                {"nom": "B", "email": "fallback@x.fr"},
            ],
        })
        assert f.email == "fallback@x.fr"

    def test_no_email_anywhere(self):
        f = Fournisseur.from_dict({"idFournisseur": 1, "nom": "X"})
        assert f.email == ""
        assert f.contacts == []

    def test_foreign_country_preserved(self):
        f = Fournisseur.from_dict({
            "idFournisseur": 1,
            "nom": "X",
            "adresse": {
                "adresse": "Via Roma 1",
                "codePostal": "00100",
                "ville": "Roma",
                "pays": "Italie",
            },
        })
        assert "Italie" in f.adresse_lignes

    def test_empty_address(self):
        f = Fournisseur.from_dict({"idFournisseur": 1, "nom": "X"})
        assert f.adresse_lignes == []

    def test_non_dict_input(self):
        f = Fournisseur.from_dict(None)  # type: ignore
        assert f.id_fournisseur == 0
        assert f.nom == ""
        assert f.contacts == []

    def test_raw_preserves_payload(self):
        """Le dict API brut est conservé pour les properties best_*."""
        payload = {
            "idFournisseur": 1, "nom": "X",
            "contactPrincipal": {"email": "legacy@x.fr"},
        }
        f = Fournisseur.from_dict(payload)
        assert f.raw == payload


class TestFournisseurBestEmail:
    def test_prefers_contact_principal(self):
        f = Fournisseur.from_dict({
            "idFournisseur": 1, "nom": "X",
            "contactPrincipal": {"email": "principal@x.fr"},
            "contact": {"email": "contact@x.fr"},
            "contacts": [{"email": "contact0@x.fr"}],
        })
        assert f.best_email == "principal@x.fr"

    def test_fallback_to_contact(self):
        f = Fournisseur.from_dict({
            "idFournisseur": 1, "nom": "X",
            "contact": {"email": "contact@x.fr"},
            "contacts": [{"email": "contact0@x.fr"}],
        })
        assert f.best_email == "contact@x.fr"

    def test_fallback_to_first_contact_with_email(self):
        f = Fournisseur.from_dict({
            "idFournisseur": 1, "nom": "X",
            "contacts": [
                {"email": ""},          # vide → skip
                {"email": "valid@x.fr"},
            ],
        })
        assert f.best_email == "valid@x.fr"

    def test_strips_whitespace(self):
        f = Fournisseur.from_dict({
            "idFournisseur": 1, "nom": "X",
            "contactPrincipal": {"email": "  trim@x.fr  "},
        })
        assert f.best_email == "trim@x.fr"

    def test_no_email_anywhere_returns_none(self):
        f = Fournisseur.from_dict({"idFournisseur": 1, "nom": "X"})
        assert f.best_email is None


class TestFournisseurBestContactName:
    def test_prefers_contact_principal(self):
        f = Fournisseur.from_dict({
            "idFournisseur": 1, "nom": "X",
            "contactPrincipal": {"prenom": "Jean", "nom": "Dupont"},
            "contact": {"prenom": "Marie", "nom": "Martin"},
        })
        assert f.best_contact_name == "Jean Dupont"

    def test_fallback_to_contact(self):
        f = Fournisseur.from_dict({
            "idFournisseur": 1, "nom": "X",
            "contact": {"prenom": "Marie", "nom": "Martin"},
        })
        assert f.best_contact_name == "Marie Martin"

    def test_only_prenom(self):
        f = Fournisseur.from_dict({
            "idFournisseur": 1, "nom": "X",
            "contactPrincipal": {"prenom": "Jean"},
        })
        assert f.best_contact_name == "Jean"

    def test_no_principal_or_contact_returns_none(self):
        """Pas de fallback sur contacts[] — historique légacy."""
        f = Fournisseur.from_dict({
            "idFournisseur": 1, "nom": "X",
            "contacts": [{"prenom": "Charlie", "nom": "Brown"}],
        })
        assert f.best_contact_name is None


class TestFournisseurFullAddressLines:
    def test_complete_field_takes_priority(self):
        """Si adresse.complete est fourni, on l'utilise tel quel (pas de splitting)."""
        f = Fournisseur.from_dict({
            "idFournisseur": 1, "nom": "Verallia",
            "adresse": {
                "complete": "31 place des Corolles\n92400 Courbevoie",
                "ligne1": "ignoré",  # ne doit pas apparaître
            },
        })
        assert f.full_address_lines == [
            "Verallia",
            "31 place des Corolles\n92400 Courbevoie",
        ]

    def test_lignes_1_to_4(self):
        f = Fournisseur.from_dict({
            "idFournisseur": 1, "nom": "X",
            "adresse": {
                "denomination": "Service commandes",
                "ligne1": "Bâtiment A",
                "ligne2": "2 rue de la Paix",
                "ligne3": "",      # vide → skip
                "ligne4": "BP 42",
                "codePostal": "75001",
                "ville": "Paris",
                "pays": "France",
            },
        })
        assert f.full_address_lines == [
            "X",
            "Service commandes",
            "Bâtiment A",
            "2 rue de la Paix",
            "BP 42",
            "75001 Paris",
            "France",
        ]

    def test_fallback_numero_rue_when_no_lignes(self):
        f = Fournisseur.from_dict({
            "idFournisseur": 1, "nom": "X",
            "adresse": {
                "numero": "42",
                "rue": "rue Principale",
                "codePostal": "13001",
                "ville": "Marseille",
            },
        })
        assert "42 rue Principale" in f.full_address_lines
        assert "13001 Marseille" in f.full_address_lines

    def test_no_address_returns_only_name(self):
        f = Fournisseur.from_dict({"idFournisseur": 1, "nom": "X"})
        assert f.full_address_lines == ["X"]

    def test_no_name_returns_empty_when_no_address(self):
        f = Fournisseur.from_dict({"idFournisseur": 1})
        assert f.full_address_lines == []
