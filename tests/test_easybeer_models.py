"""Tests for common.easybeer.models — defensive dataclass parsing."""
from __future__ import annotations

from common.easybeer.models import (
    AutonomieProduit,
    AutonomieResponse,
    MatierePremiere,
)


class TestAutonomieProduit:
    def test_full_dict(self):
        p = AutonomieProduit.from_dict({
            "libelle": "Kéfir Original",
            "autonomie": 28.5,
            "quantiteVirtuelle": 1150,
            "volume": 4.0,
        })
        assert p.libelle == "Kéfir Original"
        assert p.autonomie == 28.5
        assert p.quantite_virtuelle == 1150.0
        assert p.volume == 4.0

    def test_missing_fields_default_to_zero(self):
        p = AutonomieProduit.from_dict({"libelle": "Kéfir"})
        assert p.libelle == "Kéfir"
        assert p.autonomie == 0.0
        assert p.quantite_virtuelle == 0.0
        assert p.volume == 0.0

    def test_null_fields_default_to_zero(self):
        p = AutonomieProduit.from_dict({
            "libelle": None,
            "autonomie": None,
            "quantiteVirtuelle": None,
            "volume": None,
        })
        assert p.libelle == ""
        assert p.autonomie == 0.0

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
