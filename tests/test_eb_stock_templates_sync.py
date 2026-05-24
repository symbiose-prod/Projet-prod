"""
tests/test_eb_stock_templates_sync.py
======================================
Unit tests pour ``common/easybeer/stock_templates_sync.py``.

Couvre :
- ``_normalize_template`` : parsing de la réponse GET /stock/produit/edition/{id}
- ``sync_all_templates`` : pipeline orchestration (mocks list + detail)
- ``find_template`` : lookup unique / ambigu / absent
- ``find_template_by_code_article``

Pas de DB réelle (run_sql mocké), pas de réseau (EB client mocké).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from common.easybeer.stock_templates_sync import (
    _normalize_template,
    find_template,
    find_template_by_code_article,
    list_synced_templates,
    sync_all_templates,
)

# ─── Fixture : réponse GET /stock/produit/edition/{id} (épurée) ─────────────


def _make_detail(
    *,
    id_stock_produit: int = 113833,
    code_article: str = "SK-IGB-33-PECHE",
    id_produit: int = 42514,
    produit_libelle: str = "IGEBA Pêche - 0.0°",
    id_contenant: int = 12,
    contenant_libelle: str = "Bouteille - 0.33L",
    contenance: float = 0.33,
    id_lot: int = 4,
    lot_libelle: str = "Carton de 12",
    lot_quantite: int = 12,
    elements: list[dict] | None = None,
) -> dict:
    """Construit une réponse GET edition/{id} minimaliste mais réaliste."""
    return {
        "idStockProduit": id_stock_produit,
        "codeArticle": code_article,
        "produit": {
            "idProduit": id_produit,
            "libelle": produit_libelle,
        },
        "contenant": {
            "idContenant": id_contenant,
            "libelle": contenant_libelle,
            "libelleAvecContenance": contenant_libelle,
            "contenance": contenance,
        },
        "lot": {
            "idLot": id_lot,
            "libelle": lot_libelle,
            "quantite": lot_quantite,
        },
        "elementsConditionnement": elements if elements is not None else [
            {
                "elementMatierePremiere": {
                    "idMatierePremiere": 95498,
                    "libelle": "Capsules",
                    "code": "11A000042A",
                    "type": {"code": "CONDITIONNEMENT_CAPSULE"},
                },
                "quantite": 12.0,
            },
            {
                "elementMatierePremiere": {
                    "idMatierePremiere": 95553,
                    "libelle": "Cartons 12x33cl SYMBIOSE",
                    "code": "48508",
                    "type": {"code": "CONDITIONNEMENT_CARTON"},
                },
                "quantite": 1.0,
            },
        ],
    }


# ─── _normalize_template ────────────────────────────────────────────────────


class TestNormalizeTemplate:

    def test_extracts_all_top_level_fields(self):
        result = _normalize_template(_make_detail())
        assert result is not None
        assert result["id_stock_produit"] == 113833
        assert result["code_article"] == "SK-IGB-33-PECHE"
        assert result["id_produit"] == 42514
        assert result["produit_libelle"] == "IGEBA Pêche - 0.0°"
        assert result["id_contenant"] == 12
        assert result["contenance"] == 0.33
        assert result["lot_libelle"] == "Carton de 12"
        assert result["lot_quantite"] == 12

    def test_extracts_elements_conditionnement(self):
        result = _normalize_template(_make_detail())
        elements = result["elements_conditionnement"]
        assert len(elements) == 2
        # Capsules
        assert elements[0]["idMatierePremiere"] == 95498
        assert elements[0]["libelle"] == "Capsules"
        assert elements[0]["type"] == "CONDITIONNEMENT_CAPSULE"
        assert elements[0]["quantite"] == 12.0
        # Cartons
        assert elements[1]["idMatierePremiere"] == 95553
        assert elements[1]["type"] == "CONDITIONNEMENT_CARTON"

    def test_returns_none_if_code_article_missing(self):
        detail = _make_detail()
        detail["codeArticle"] = ""
        assert _normalize_template(detail) is None

    def test_returns_none_if_id_produit_missing(self):
        detail = _make_detail()
        detail["produit"] = {}
        assert _normalize_template(detail) is None

    def test_handles_missing_contenant(self):
        detail = _make_detail()
        detail["contenant"] = {}
        result = _normalize_template(detail)
        assert result is not None
        assert result["id_contenant"] is None
        assert result["contenance"] is None

    def test_handles_missing_elements_conditionnement(self):
        detail = _make_detail(elements=[])
        result = _normalize_template(detail)
        assert result is not None
        assert result["elements_conditionnement"] == []

    def test_skips_elements_without_idMatierePremiere(self):
        detail = _make_detail(elements=[
            {"elementMatierePremiere": {"idMatierePremiere": 95498, "libelle": "Capsules"}, "quantite": 12.0},
            {"elementMatierePremiere": {"libelle": "broken — no id"}, "quantite": 1.0},
        ])
        result = _normalize_template(detail)
        assert len(result["elements_conditionnement"]) == 1  # le 2e ignoré
        assert result["elements_conditionnement"][0]["idMatierePremiere"] == 95498


# ─── sync_all_templates ─────────────────────────────────────────────────────


class TestSyncAllTemplates:

    @patch("common.easybeer.stock_templates_sync._upsert_template")
    @patch("common.easybeer.stock_templates_sync._stocks.get_stock_produit_detail")
    @patch("common.easybeer.stock_templates_sync._list_stock_produit_ids")
    def test_happy_path(
        self,
        mock_list: MagicMock,
        mock_detail: MagicMock,
        mock_upsert: MagicMock,
    ):
        mock_list.return_value = [113833, 167607, 200001]
        mock_detail.side_effect = [
            _make_detail(id_stock_produit=113833, code_article="SK-IGB-33-PECHE"),
            _make_detail(
                id_stock_produit=167607,
                code_article="SK-KDF-33-ORI",
                id_produit=42397,
            ),
            _make_detail(
                id_stock_produit=200001,
                code_article="SK-KDF-75-ORI",
                contenance=0.75,
                lot_quantite=6,
                lot_libelle="Carton de 6",
            ),
        ]

        result = sync_all_templates(tenant_id="tenant-A")

        assert result == {"total": 3, "upserted": 3, "skipped": 0, "errors": 0}
        assert mock_upsert.call_count == 3
        # Vérifie qu'on a bien passé le tenant + chaque template
        upserted_codes = {call.args[1]["code_article"] for call in mock_upsert.call_args_list}
        assert upserted_codes == {"SK-IGB-33-PECHE", "SK-KDF-33-ORI", "SK-KDF-75-ORI"}

    @patch("common.easybeer.stock_templates_sync._upsert_template")
    @patch("common.easybeer.stock_templates_sync._stocks.get_stock_produit_detail")
    @patch("common.easybeer.stock_templates_sync._list_stock_produit_ids")
    def test_skips_templates_without_code_article(
        self,
        mock_list: MagicMock,
        mock_detail: MagicMock,
        mock_upsert: MagicMock,
    ):
        broken = _make_detail()
        broken["codeArticle"] = ""  # invalide
        mock_list.return_value = [1, 2]
        mock_detail.side_effect = [
            _make_detail(id_stock_produit=1),
            broken,
        ]

        result = sync_all_templates(tenant_id="tenant-A")
        assert result["total"] == 2
        assert result["upserted"] == 1
        assert result["skipped"] == 1
        assert mock_upsert.call_count == 1

    @patch("common.easybeer.stock_templates_sync._upsert_template")
    @patch("common.easybeer.stock_templates_sync._stocks.get_stock_produit_detail")
    @patch("common.easybeer.stock_templates_sync._list_stock_produit_ids")
    def test_continues_on_per_item_error(
        self,
        mock_list: MagicMock,
        mock_detail: MagicMock,
        mock_upsert: MagicMock,
    ):
        """Si un GET edition/{id} fail, on log et on continue avec les autres."""
        from common.easybeer._client import EasyBeerError
        mock_list.return_value = [1, 2, 3]
        mock_detail.side_effect = [
            _make_detail(id_stock_produit=1),
            EasyBeerError("simulated network failure"),
            _make_detail(id_stock_produit=3, code_article="SK-OK-3"),
        ]

        result = sync_all_templates(tenant_id="tenant-A")
        assert result["total"] == 3
        assert result["upserted"] == 2
        assert result["errors"] == 1
        assert mock_upsert.call_count == 2

    def test_raises_if_tenant_id_missing(self, monkeypatch):
        """Sans tenant_id et sans current_tenant_id, on doit raise clairement."""
        monkeypatch.setattr(
            "common.easybeer.stock_templates_sync.current_tenant_id",
            lambda: "",
        )
        with pytest.raises(RuntimeError, match="tenant_id manquant"):
            sync_all_templates(tenant_id=None)


# ─── Lookups ────────────────────────────────────────────────────────────────


class TestFindTemplate:

    @patch("common.easybeer.stock_templates_sync.run_sql")
    def test_unique_match_returns_template(self, mock_sql: MagicMock):
        mock_sql.return_value = [
            {
                "id_stock_produit": 167607,
                "code_article": "SK-KDF-33-ORI",
                "id_produit": 42397,
                "produit_libelle": "Kéfir de fruits Original - 0.0°",
                "id_contenant": 12,
                "contenant_libelle": "Bouteille - 0.33L",
                "contenance": 0.33,
                "id_lot": 3,
                "lot_libelle": "Carton de 6",
                "lot_quantite": 6,
                "elements_conditionnement": [],
                "synced_at": None,
            },
        ]
        result = find_template(
            tenant_id="t", id_produit=42397, contenance=0.33, lot_quantite=6,
        )
        assert result is not None
        assert result["code_article"] == "SK-KDF-33-ORI"

    @patch("common.easybeer.stock_templates_sync.run_sql")
    def test_no_match_returns_none(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        assert find_template(
            tenant_id="t", id_produit=999, contenance=0.33, lot_quantite=6,
        ) is None

    @patch("common.easybeer.stock_templates_sync.run_sql")
    def test_ambiguous_returns_none(self, mock_sql: MagicMock):
        """Cas SAFT vs Verralia : 2 templates, on ne tranche pas ici."""
        mock_sql.return_value = [{"code_article": "A"}, {"code_article": "B"}]
        assert find_template(
            tenant_id="t", id_produit=42397, contenance=0.75, lot_quantite=6,
        ) is None


class TestFindTemplateByCodeArticle:

    @patch("common.easybeer.stock_templates_sync.run_sql")
    def test_exact_match(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"code_article": "SK-KDF-33-ORI"}]
        result = find_template_by_code_article(tenant_id="t", code_article="SK-KDF-33-ORI")
        assert result == {"code_article": "SK-KDF-33-ORI"}

    @patch("common.easybeer.stock_templates_sync.run_sql")
    def test_no_match(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        assert find_template_by_code_article(tenant_id="t", code_article="DOES-NOT-EXIST") is None


class TestListSyncedTemplates:

    @patch("common.easybeer.stock_templates_sync.run_sql")
    def test_returns_list_of_dicts(self, mock_sql: MagicMock):
        mock_sql.return_value = [
            {"code_article": "SK-KDF-33-ORI", "id_produit": 42397, "synced_at": None},
            {"code_article": "SK-KDF-75-ORI", "id_produit": 42397, "synced_at": None},
        ]
        result = list_synced_templates(tenant_id="t")
        assert len(result) == 2
        assert all(isinstance(r, dict) for r in result)

    @patch("common.easybeer.stock_templates_sync.current_tenant_id", return_value="")
    @patch("common.easybeer.stock_templates_sync.run_sql")
    def test_returns_empty_if_no_tenant(self, mock_sql: MagicMock, _mock_ctx: MagicMock):
        """Si ni param ni current_tenant_id() ne fournit un tenant, on retourne []."""
        result = list_synced_templates(tenant_id="")
        assert result == []
        mock_sql.assert_not_called()
