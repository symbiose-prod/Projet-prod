"""
tests/test_mise_en_bouteille_orchestrator.py
=============================================
Tests pour ``common/services/mise_en_bouteille_orchestrator.py``.

Couvre le pipeline complet :
- Validation du payload léger
- ``get_brassin_detail`` (mocké)
- ``resolve_bottle_stock`` (mocké)
- 2 appels EB séquentiels : deduction puis mise-en-bouteille
- Injection de la BOM calculée

Pas de DB réelle ni de HTTP.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from common.services.mise_en_bouteille_orchestrator import execute_mise_en_bouteille

# ─── Fixtures ─────────────────────────────────────────────────────────────


def _light_payload(**overrides):
    """Payload léger produit par build_mise_en_bouteille_payload."""
    base = {
        "idBrassin": 259288,
        "tenantId": "tenant-A",
        "numeroLot": "KDF18052026",
        "dateMiseEnBouteille": "2026-05-23T18:04:01.000Z",
        "items": [{"marque": "SYMBIOSE", "fmt": "6x33", "cartons": 199}],
    }
    base.update(overrides)
    return base


def _brassin_full(**overrides):
    """Brassin EB complet (épuré pour tests)."""
    base = {
        "idBrassin": 259288,
        "nom": "KDF18052026",
        "produit": {"idProduit": 42397, "libelle": "Kéfir de fruits Original"},
        "volume": 1300,
        "volumeRestant": 1300,
        "modeleElevage": {},
        "modelesStockProduitBouteille": [
            {
                "libelle": "FERMENT STATION",
                "modelesFils": [
                    {
                        "idStockBouteille": 111377,
                        "libelle": "Bouteille - 0.33L",
                        "contenance": 0.33,
                    },
                    {
                        "idStockBouteille": 111687,
                        "libelle": "Bouteille 75cl Verralia - 0.75L",
                        "contenance": 0.75,
                    },
                ],
            },
        ],
    }
    base.update(overrides)
    return base


def _mock_resolution(*, id_stock=111377, contenance=0.33):
    from common.services.bottle_stock_resolver import BottleStockResolution
    return BottleStockResolution(
        id_stock_bouteille=id_stock,
        contenant_libelle="Bouteille - 0.33L",
        contenance=contenance,
        code_article="SK-KDF-33-ORI",
        id_stock_produit=167607,
        lot_quantite=6,
        elements_conditionnement=[],
    )


# ─── Validation du payload léger ─────────────────────────────────────────


class TestPayloadValidation:

    def test_raises_if_no_idBrassin(self):
        with pytest.raises(ValueError, match="idBrassin"):
            execute_mise_en_bouteille({"tenantId": "t", "items": [{}]})

    def test_raises_if_no_tenantId(self):
        with pytest.raises(ValueError, match="tenantId"):
            execute_mise_en_bouteille({"idBrassin": 1, "items": [{}]})

    def test_raises_if_no_items(self):
        with pytest.raises(ValueError, match="items"):
            execute_mise_en_bouteille({
                "idBrassin": 1, "tenantId": "t", "items": [],
                "numeroLot": "L", "dateMiseEnBouteille": "now",
            })

    def test_raises_if_no_numeroLot(self):
        with pytest.raises(ValueError, match="numeroLot"):
            execute_mise_en_bouteille({
                "idBrassin": 1, "tenantId": "t",
                "items": [{"marque": "X", "fmt": "6x33", "cartons": 1}],
                "dateMiseEnBouteille": "now",
            })


# ─── Validation des données brassin ───────────────────────────────────────


class TestBrassinFetchAndValidation:

    @patch("common.services.mise_en_bouteille_orchestrator.execute_endpoint")
    @patch("common.services.mise_en_bouteille_orchestrator.resolve_bottle_stock")
    @patch("common.services.mise_en_bouteille_orchestrator.get_brassin_detail")
    def test_raises_if_brassin_not_found(
        self,
        mock_get_brassin: MagicMock,
        _mock_resolve: MagicMock,
        _mock_execute: MagicMock,
    ):
        mock_get_brassin.return_value = None
        with pytest.raises(ValueError, match="introuvable"):
            execute_mise_en_bouteille(_light_payload())

    @patch("common.services.mise_en_bouteille_orchestrator.execute_endpoint")
    @patch("common.services.mise_en_bouteille_orchestrator.resolve_bottle_stock")
    @patch("common.services.mise_en_bouteille_orchestrator.get_brassin_detail")
    def test_raises_if_no_modelesStockProduitBouteille(
        self,
        mock_get_brassin: MagicMock,
        _mock_resolve: MagicMock,
        _mock_execute: MagicMock,
    ):
        brassin = _brassin_full()
        brassin["modelesStockProduitBouteille"] = []
        mock_get_brassin.return_value = brassin
        with pytest.raises(ValueError, match="modelesStockProduitBouteille"):
            execute_mise_en_bouteille(_light_payload())

    @patch("common.services.mise_en_bouteille_orchestrator.execute_endpoint")
    @patch("common.services.mise_en_bouteille_orchestrator.resolve_bottle_stock")
    @patch("common.services.mise_en_bouteille_orchestrator.get_brassin_detail")
    def test_raises_if_no_idProduit(
        self,
        mock_get_brassin: MagicMock,
        _mock_resolve: MagicMock,
        _mock_execute: MagicMock,
    ):
        brassin = _brassin_full()
        brassin["produit"] = {}
        mock_get_brassin.return_value = brassin
        with pytest.raises(ValueError, match="idProduit"):
            execute_mise_en_bouteille(_light_payload())


# ─── Résolution + 2 appels EB ─────────────────────────────────────────────


class TestPipeline:

    @patch("common.services.mise_en_bouteille_orchestrator.execute_endpoint")
    @patch("common.services.mise_en_bouteille_orchestrator.resolve_bottle_stock")
    @patch("common.services.mise_en_bouteille_orchestrator.get_brassin_detail")
    def test_happy_path_two_eb_calls_in_order(
        self,
        mock_get_brassin: MagicMock,
        mock_resolve: MagicMock,
        mock_execute: MagicMock,
    ):
        """Le pipeline appelle deduction PUIS mise-en-bouteille, dans cet ordre."""
        mock_get_brassin.return_value = _brassin_full()
        mock_resolve.return_value = _mock_resolution()
        mock_execute.side_effect = [
            {  # deduction → BOM
                "modelesStocksMiseEnBouteille": [
                    {"type": "BOUTEILLE", "idStockBouteille": 111377, "quantite": 1194.0},
                    {"type": "MP", "idMatierePremiere": 95498, "quantite": 1194.0},
                ],
            },
            {"message": "", "map": {}},  # mise-en-bouteille success
        ]

        result = execute_mise_en_bouteille(_light_payload())

        assert mock_execute.call_count == 2
        paths = [c.kwargs["path"] for c in mock_execute.call_args_list]
        assert paths == [
            "brassin/deduction-stocks-conditionnement",
            "brassin/mise-en-bouteille",
        ]
        assert result == {"message": "", "map": {}}

    @patch("common.services.mise_en_bouteille_orchestrator.execute_endpoint")
    @patch("common.services.mise_en_bouteille_orchestrator.resolve_bottle_stock")
    @patch("common.services.mise_en_bouteille_orchestrator.get_brassin_detail")
    def test_deduction_bom_is_injected_in_mise_en_bouteille(
        self,
        mock_get_brassin: MagicMock,
        mock_resolve: MagicMock,
        mock_execute: MagicMock,
    ):
        """La BOM retournée par deduction doit se retrouver dans le call mise-en-bouteille."""
        mock_get_brassin.return_value = _brassin_full()
        mock_resolve.return_value = _mock_resolution()
        bom = [
            {"type": "BOUTEILLE", "idStockBouteille": 111377, "quantite": 1194.0},
            {"type": "MP", "idMatierePremiere": 95498, "quantite": 1194.0},
            {"type": "MP", "idMatierePremiere": 95553, "quantite": 199.0},
            {"type": "MP", "idMatierePremiere": 95552, "quantite": 1194.0},
        ]
        mock_execute.side_effect = [{"modelesStocksMiseEnBouteille": bom}, {}]

        execute_mise_en_bouteille(_light_payload())

        # Le 2e call doit avoir la BOM identique du 1er
        final_payload = mock_execute.call_args_list[1].kwargs["payload"]
        assert final_payload["modelesStocksMiseEnBouteille"] == bom

    @patch("common.services.mise_en_bouteille_orchestrator.execute_endpoint")
    @patch("common.services.mise_en_bouteille_orchestrator.resolve_bottle_stock")
    @patch("common.services.mise_en_bouteille_orchestrator.get_brassin_detail")
    def test_unresolved_item_raises_value_error(
        self,
        mock_get_brassin: MagicMock,
        mock_resolve: MagicMock,
        _mock_execute: MagicMock,
    ):
        """Si resolve_bottle_stock renvoie None → ValueError actionnable."""
        mock_get_brassin.return_value = _brassin_full()
        mock_resolve.return_value = None

        with pytest.raises(ValueError, match="résolution impossible"):
            execute_mise_en_bouteille(_light_payload())

    @patch("common.services.mise_en_bouteille_orchestrator.execute_endpoint")
    @patch("common.services.mise_en_bouteille_orchestrator.resolve_bottle_stock")
    @patch("common.services.mise_en_bouteille_orchestrator.get_brassin_detail")
    def test_mise_en_bouteille_uses_allow_empty_2xx(
        self,
        mock_get_brassin: MagicMock,
        mock_resolve: MagicMock,
        mock_execute: MagicMock,
    ):
        """EB peut renvoyer body vide sur succès — allow_empty_2xx doit être True."""
        mock_get_brassin.return_value = _brassin_full()
        mock_resolve.return_value = _mock_resolution()
        mock_execute.side_effect = [{"modelesStocksMiseEnBouteille": []}, {}]

        execute_mise_en_bouteille(_light_payload())

        # Le call mise-en-bouteille (le 2e) doit passer allow_empty_2xx=True
        mise_call = mock_execute.call_args_list[1]
        assert mise_call.kwargs.get("allow_empty_2xx") is True

    @patch("common.services.mise_en_bouteille_orchestrator.execute_endpoint")
    @patch("common.services.mise_en_bouteille_orchestrator.resolve_bottle_stock")
    @patch("common.services.mise_en_bouteille_orchestrator.get_brassin_detail")
    def test_unused_fils_are_sent_with_null_quantite(
        self,
        mock_get_brassin: MagicMock,
        mock_resolve: MagicMock,
        mock_execute: MagicMock,
    ):
        """Les fils brassin non utilisés sont quand même envoyés (quantite=None)
        — EB UI fait pareil et l'attend probablement."""
        # 2 fils dispo dans le brassin (33cl + 75cl), mais on n'utilise que le 33cl
        mock_get_brassin.return_value = _brassin_full()
        mock_resolve.return_value = _mock_resolution(id_stock=111377)
        mock_execute.side_effect = [{"modelesStocksMiseEnBouteille": []}, {}]

        execute_mise_en_bouteille(_light_payload())

        # Inspecte le payload envoyé à deduction-stocks (1er call)
        deduction_payload = mock_execute.call_args_list[0].kwargs["payload"]
        fils = deduction_payload["modelesStockProduitBouteille"][0]["modelesFils"]
        # On devrait avoir 2 fils : 1 avec quantité (utilisé), 1 avec None (pas utilisé)
        used = [f for f in fils if f["quantiteMiseEnBouteille"] is not None]
        unused = [f for f in fils if f["quantiteMiseEnBouteille"] is None]
        assert len(used) == 1
        assert used[0]["idStockBouteille"] == 111377
        assert used[0]["quantiteMiseEnBouteille"] == 199
        assert len(unused) == 1
        assert unused[0]["idStockBouteille"] == 111687  # 75cl non utilisé


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
