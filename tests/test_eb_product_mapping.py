"""Tests for common/services/eb_product_mapping.py."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from common.services.eb_product_mapping import (
    GtinIndexEntry,
    LotMarqueFmtResolution,
    build_gtin_index,
    lookup_gtin,
    normalize_gtin,
    resolve_lot_marque_fmt,
)

# ─── normalize_gtin ───────────────────────────────────────────────────────


class TestNormalizeGtin:

    def test_strips_non_digits(self):
        assert normalize_gtin(" 3770 014427014 ") == "3770014427014"

    def test_empty(self):
        assert normalize_gtin("") == ""
        assert normalize_gtin(None) == ""

    def test_only_letters(self):
        assert normalize_gtin("abc") == ""


# ─── build_gtin_index ─────────────────────────────────────────────────────


class TestBuildGtinIndex:

    def test_empty_matrice(self):
        assert build_gtin_index({}) == {}

    def test_full_entry(self):
        matrice = {
            "produits": [
                {
                    "codesBarres": [
                        {
                            "code": "3770014427014",
                            "modeleProduit": {"idProduit": 100},
                            "modeleContenant": {"idContenant": 50, "contenance": 0.33},
                            "modeleLot": {"libelle": "Carton de 12"},
                        },
                    ],
                }
            ]
        }
        index = build_gtin_index(matrice)

        entry = index["3770014427014"]
        assert entry.id_produit == 100
        assert entry.id_contenant == 50
        assert entry.contenance_l == 0.33
        assert entry.lot_libelle == "Carton de 12"

        # Ref6 indexé aussi
        assert "427014" in index
        assert index["427014"].id_produit == 100

    def test_minimal_entry_without_contenant(self):
        matrice = {
            "produits": [{
                "codesBarres": [{
                    "code": "1234567890",
                    "modeleProduit": {"idProduit": 5},
                }],
            }]
        }
        index = build_gtin_index(matrice)
        entry = index["1234567890"]
        assert entry.id_produit == 5
        assert entry.id_contenant is None
        assert entry.contenance_l is None

    def test_skips_entries_without_id_produit(self):
        matrice = {"produits": [{"codesBarres": [{"code": "999"}]}]}
        assert build_gtin_index(matrice) == {}


# ─── lookup_gtin ──────────────────────────────────────────────────────────


class TestLookupGtin:

    def _idx(self):
        return {
            "3770014427014": GtinIndexEntry(
                id_produit=100, id_contenant=50, contenance_l=0.33, lot_libelle=None,
            ),
            "427014": GtinIndexEntry(
                id_produit=100, id_contenant=50, contenance_l=0.33, lot_libelle=None,
            ),
        }

    def test_full_gtin(self):
        idx = self._idx()
        assert lookup_gtin(idx, "3770014427014").id_produit == 100

    def test_ref6_fallback(self):
        idx = self._idx()
        assert lookup_gtin(idx, "427014").id_produit == 100

    def test_with_spaces(self):
        idx = self._idx()
        assert lookup_gtin(idx, " 3770 0144 27014 ").id_produit == 100

    def test_unknown(self):
        idx = self._idx()
        assert lookup_gtin(idx, "9999999999999") is None

    def test_empty(self):
        idx = self._idx()
        assert lookup_gtin(idx, "") is None
        assert lookup_gtin(idx, None) is None


# ─── resolve_lot_marque_fmt ───────────────────────────────────────────────


class TestResolveLotMarqueFmt:

    @patch("common.services.eb_product_mapping.run_sql")
    def test_resolves_both_bouteille_and_carton(self, mock_sql: MagicMock):
        """Cas nominal : gtin_uvc (bouteille) ET ean (carton) sont dans la matrice EB."""
        mock_sql.return_value = [{
            "ean": "3770014427014",       # GTIN carton
            "gtin_uvc": "3770014427021",  # GTIN bouteille
            "lot": "KMA15052026",
            "fmt": "12x33",
            "marque": "NIKO",
            "designation": "Kéfir Mangue Passion",
            "pcb": 12,
        }]
        gtin_index = {
            "3770014427021": GtinIndexEntry(
                id_produit=200, id_contenant=60, contenance_l=0.33, lot_libelle=None,
            ),
            "3770014427014": GtinIndexEntry(
                id_produit=200, id_contenant=999, contenance_l=0.33, lot_libelle="Carton de 12",
            ),
        }

        result = resolve_lot_marque_fmt(
            tenant_id="t1",
            lot="KMA15052026",
            marque="NIKO",
            fmt="12x33",
            gtin_index=gtin_index,
        )

        assert isinstance(result, LotMarqueFmtResolution)
        assert result.id_produit == 200
        assert result.id_contenant_bouteille == 60
        assert result.id_contenant_carton == 999  # Carton de 12
        assert result.contenance_l == 0.33
        assert result.pcb == 12
        assert result.gtin_uvc == "3770014427021"
        assert result.ean == "3770014427014"

    @patch("common.services.eb_product_mapping.run_sql")
    def test_only_bouteille_in_matrice(self, mock_sql: MagicMock):
        """Si seul le gtin_uvc est dans la matrice (ean non synchronisé), on
        retourne id_contenant_bouteille mais id_contenant_carton = None."""
        mock_sql.return_value = [{
            "ean": "3770014427014",
            "gtin_uvc": "3770014427021",
            "pcb": 12,
        }]
        gtin_index = {
            "3770014427021": GtinIndexEntry(
                id_produit=200, id_contenant=60, contenance_l=0.33, lot_libelle=None,
            ),
            # ean absent
        }
        result = resolve_lot_marque_fmt(
            tenant_id="t1", lot="L1", marque="NIKO", fmt="12x33",
            gtin_index=gtin_index,
        )
        assert result.id_contenant_bouteille == 60
        assert result.id_contenant_carton is None

    @patch("common.services.eb_product_mapping.run_sql")
    def test_only_carton_in_matrice(self, mock_sql: MagicMock):
        """Symétrique : seul l'ean (carton) est dans la matrice."""
        mock_sql.return_value = [{
            "ean": "3770014427014",
            "gtin_uvc": "3770014427021",
            "pcb": 12,
        }]
        gtin_index = {
            "3770014427014": GtinIndexEntry(
                id_produit=200, id_contenant=999, contenance_l=0.33, lot_libelle=None,
            ),
        }
        result = resolve_lot_marque_fmt(
            tenant_id="t1", lot="L1", marque="NIKO", fmt="12x33",
            gtin_index=gtin_index,
        )
        assert result.id_contenant_bouteille is None
        assert result.id_contenant_carton == 999
        assert result.id_produit == 200

    @patch("common.services.eb_product_mapping.run_sql")
    def test_returns_none_if_no_etiquette(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        result = resolve_lot_marque_fmt(
            tenant_id="t1", lot="L1", marque="NIKO", fmt="12x33",
            gtin_index={},
        )
        assert result is None

    @patch("common.services.eb_product_mapping.run_sql")
    def test_returns_none_if_no_gtin_in_matrice(self, mock_sql: MagicMock):
        """Ni gtin_uvc ni ean trouvés dans la matrice → None."""
        mock_sql.return_value = [{
            "ean": "3770014427014",
            "gtin_uvc": "3770014427021",
            "pcb": 12,
        }]
        # gtin_index vide → ni l'un ni l'autre trouvable
        result = resolve_lot_marque_fmt(
            tenant_id="t1", lot="L1", marque="NIKO", fmt="12x33",
            gtin_index={},
        )
        assert result is None

    def test_returns_none_for_empty_inputs(self):
        assert resolve_lot_marque_fmt(
            tenant_id="t1", lot="", marque="NIKO", fmt="12x33", gtin_index={},
        ) is None
        assert resolve_lot_marque_fmt(
            tenant_id="t1", lot="L1", marque="", fmt="12x33", gtin_index={},
        ) is None
        assert resolve_lot_marque_fmt(
            tenant_id="t1", lot="L1", marque="NIKO", fmt="", gtin_index={},
        ) is None
