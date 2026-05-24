"""
tests/test_bottle_stock_resolver.py
====================================
Tests unitaires pour ``common/services/bottle_stock_resolver.py``.

Couvre :
- ``parse_fmt`` : parsing de "6x33", "12x33", "4x75", cas dégénérés
- ``resolve_bottle_stock`` : cascade complète (lookup → disambiguate → match fils)
- Cas d'ambiguïté (Verralia vs SAFT) résolus par marque ou par fmt

Pas de DB réelle (``run_sql`` mocké).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from common.services.bottle_stock_resolver import (
    BottleStockResolution,
    parse_fmt,
    resolve_bottle_stock,
)

# ─── parse_fmt ──────────────────────────────────────────────────────────────


class TestParseFmt:

    def test_6x33(self):
        assert parse_fmt("6x33") == (6, 0.33)

    def test_12x33(self):
        assert parse_fmt("12x33") == (12, 0.33)

    def test_4x75(self):
        assert parse_fmt("4x75") == (4, 0.75)

    def test_6x75(self):
        assert parse_fmt("6x75") == (6, 0.75)

    def test_handles_uppercase_X(self):
        assert parse_fmt("6X33") == (6, 0.33)

    def test_handles_whitespace(self):
        assert parse_fmt("  6 x 33  ") == (6, 0.33)

    def test_empty_returns_none(self):
        assert parse_fmt("") is None
        assert parse_fmt(None) is None  # type: ignore

    def test_invalid_format_returns_none(self):
        assert parse_fmt("foo") is None
        assert parse_fmt("6x") is None
        assert parse_fmt("x33") is None
        assert parse_fmt("6x33x4") is None

    def test_zero_or_negative_returns_none(self):
        assert parse_fmt("0x33") is None
        assert parse_fmt("6x0") is None


# ─── Fixtures pour resolve_bottle_stock ──────────────────────────────────────


def _make_template(
    *,
    code_article: str = "SK-KDF-33-ORI",
    id_stock_produit: int = 167607,
    id_produit: int = 42397,
    contenance: float = 0.33,
    lot_quantite: int = 6,
    contenant_libelle: str = "Bouteille - 0.33L",
    elements: list | None = None,
) -> dict:
    return {
        "id_stock_produit": id_stock_produit,
        "code_article": code_article,
        "id_produit": id_produit,
        "produit_libelle": "Kéfir de fruits Original - 0.0°",
        "id_contenant": 12 if contenance == 0.33 else 13,
        "contenant_libelle": contenant_libelle,
        "contenance": contenance,
        "id_lot": 3,
        "lot_libelle": f"Carton de {lot_quantite}",
        "lot_quantite": lot_quantite,
        "elements_conditionnement": elements or [
            {
                "idMatierePremiere": 95498,
                "libelle": "Capsules",
                "type": "CONDITIONNEMENT_CAPSULE",
                "quantite": float(lot_quantite),
            },
        ],
    }


def _make_brassin_fils() -> list[dict]:
    """Fils typiques du brassin KDF Original : 3 stocks bouteille."""
    return [
        {
            "idStockBouteille": 111377,
            "libelle": "Bouteille - 0.33L",
            "contenance": 0.33,
        },
        {
            "idStockBouteille": 111687,
            "libelle": "Bouteille 75cl EAU GAZEUSE - 0.75L",  # = Verralia côté EB
            "contenance": 0.75,
        },
        {
            "idStockBouteille": 131955,
            "libelle": "Bouteille 75cl SAFT - 0.75L",
            "contenance": 0.75,
        },
    ]


# ─── resolve_bottle_stock : happy paths ─────────────────────────────────────


class TestResolveBottleStockHappyPaths:

    @patch("common.services.bottle_stock_resolver.run_sql")
    def test_33cl_symbiose_unique_match(self, mock_sql: MagicMock):
        """6x33 SYMBIOSE → 1 template, 1 fil 33cl → match direct."""
        mock_sql.return_value = [_make_template(code_article="SK-KDF-33-ORI")]
        result = resolve_bottle_stock(
            tenant_id="t",
            brassin_fils=_make_brassin_fils(),
            id_produit=42397,
            fmt="6x33",
            marque="SYMBIOSE",
        )
        assert isinstance(result, BottleStockResolution)
        assert result.id_stock_bouteille == 111377
        assert result.code_article == "SK-KDF-33-ORI"
        assert result.lot_quantite == 6
        assert result.contenance == 0.33

    @patch("common.services.bottle_stock_resolver.run_sql")
    def test_12x33_symbiose(self, mock_sql: MagicMock):
        """Carton de 12 bouteilles 33cl Symbiose."""
        mock_sql.return_value = [
            _make_template(
                code_article="SK-KDF-33-ORI",
                lot_quantite=12,
                contenance=0.33,
                contenant_libelle="Bouteille - 0.33L",
            ),
        ]
        result = resolve_bottle_stock(
            tenant_id="t",
            brassin_fils=_make_brassin_fils(),
            id_produit=42397,
            fmt="12x33",
            marque="SYMBIOSE",
        )
        assert result is not None
        assert result.id_stock_bouteille == 111377
        assert result.lot_quantite == 12

    @patch("common.services.bottle_stock_resolver.run_sql")
    def test_6x75_symbiose_verralia(self, mock_sql: MagicMock):
        """6x75 SYMBIOSE Verralia : ambigu en table (SK + ?), désambiguïsation par marque."""
        mock_sql.return_value = [
            _make_template(
                code_article="SK-KDF-75-ORI",
                contenance=0.75,
                lot_quantite=6,
                contenant_libelle="Bouteille 75cl Verralia - 0.75L",
            ),
        ]
        result = resolve_bottle_stock(
            tenant_id="t",
            brassin_fils=_make_brassin_fils(),
            id_produit=42397,
            fmt="6x75",
            marque="SYMBIOSE",
        )
        assert result is not None
        # Match keyword "verralia" → fil 111687 (libellé "EAU GAZEUSE")
        assert result.id_stock_bouteille == 111687

    @patch("common.services.bottle_stock_resolver.run_sql")
    def test_6x75_niko_saft(self, mock_sql: MagicMock):
        """6x75 NIKO → SAFT."""
        mock_sql.return_value = [
            _make_template(
                code_article="NIKO-KDF-75-GIN",
                contenance=0.75,
                lot_quantite=6,
                contenant_libelle="Bouteille 75cl SAFT - 0.75L",
            ),
        ]
        result = resolve_bottle_stock(
            tenant_id="t",
            brassin_fils=_make_brassin_fils(),
            id_produit=42397,
            fmt="6x75",
            marque="NIKO",
        )
        assert result is not None
        assert result.id_stock_bouteille == 131955  # SAFT
        assert result.code_article == "NIKO-KDF-75-GIN"

    @patch("common.services.bottle_stock_resolver.run_sql")
    def test_4x75_symbiose_pack_saft(self, mock_sql: MagicMock):
        """Pack de 4 Symbiose 75cl → SAFT (heuristique fmt 4x)."""
        mock_sql.return_value = [
            _make_template(
                code_article="SK-KDF-75-GIN",
                contenance=0.75,
                lot_quantite=4,
                contenant_libelle="Bouteille 75cl SAFT - 0.75L",
            ),
        ]
        result = resolve_bottle_stock(
            tenant_id="t",
            brassin_fils=_make_brassin_fils(),
            id_produit=42397,
            fmt="4x75",
            marque="SYMBIOSE",
        )
        assert result is not None
        assert result.id_stock_bouteille == 131955  # SAFT


# ─── resolve_bottle_stock : disambiguation ───────────────────────────────────


class TestResolveBottleStockDisambiguation:

    @patch("common.services.bottle_stock_resolver.run_sql")
    def test_75cl_two_templates_niko_picks_niko(self, mock_sql: MagicMock):
        """2 templates (SK-* + NIKO-*) pour 6x75 → marque=NIKO retourne NIKO-*."""
        mock_sql.return_value = [
            _make_template(
                code_article="SK-KDF-75-ORI",
                contenance=0.75, lot_quantite=6,
                contenant_libelle="Bouteille 75cl Verralia - 0.75L",
            ),
            _make_template(
                code_article="NIKO-KDF-75-GIN",
                contenance=0.75, lot_quantite=6,
                contenant_libelle="Bouteille 75cl SAFT - 0.75L",
            ),
        ]
        result = resolve_bottle_stock(
            tenant_id="t",
            brassin_fils=_make_brassin_fils(),
            id_produit=42397,
            fmt="6x75",
            marque="NIKO",
        )
        assert result is not None
        assert result.code_article == "NIKO-KDF-75-GIN"
        assert result.id_stock_bouteille == 131955

    @patch("common.services.bottle_stock_resolver.run_sql")
    def test_75cl_two_templates_symbiose_picks_sk(self, mock_sql: MagicMock):
        """2 templates → marque=SYMBIOSE retourne SK-*."""
        mock_sql.return_value = [
            _make_template(
                code_article="SK-KDF-75-ORI",
                contenance=0.75, lot_quantite=6,
                contenant_libelle="Bouteille 75cl Verralia - 0.75L",
            ),
            _make_template(
                code_article="NIKO-KDF-75-GIN",
                contenance=0.75, lot_quantite=6,
                contenant_libelle="Bouteille 75cl SAFT - 0.75L",
            ),
        ]
        result = resolve_bottle_stock(
            tenant_id="t",
            brassin_fils=_make_brassin_fils(),
            id_produit=42397,
            fmt="6x75",
            marque="SYMBIOSE",
        )
        assert result is not None
        assert result.code_article == "SK-KDF-75-ORI"
        assert result.id_stock_bouteille == 111687  # Verralia/EAU GAZEUSE


# ─── resolve_bottle_stock : échecs ──────────────────────────────────────────


class TestResolveBottleStockFailures:

    def test_invalid_fmt_returns_none(self):
        assert resolve_bottle_stock(
            tenant_id="t",
            brassin_fils=_make_brassin_fils(),
            id_produit=42397,
            fmt="bad",
            marque="SYMBIOSE",
        ) is None

    @patch("common.services.bottle_stock_resolver.run_sql")
    def test_no_template_in_db_returns_none(self, mock_sql: MagicMock):
        """Produit inconnu → table vide → None."""
        mock_sql.return_value = []
        result = resolve_bottle_stock(
            tenant_id="t",
            brassin_fils=_make_brassin_fils(),
            id_produit=99999,
            fmt="6x33",
            marque="SYMBIOSE",
        )
        assert result is None

    @patch("common.services.bottle_stock_resolver.run_sql")
    def test_no_matching_fil_returns_none(self, mock_sql: MagicMock):
        """Template existe mais aucun fil brassin ne matche."""
        mock_sql.return_value = [
            _make_template(
                code_article="SK-XXX-75-WEIRD",
                contenance=0.75, lot_quantite=6,
                contenant_libelle="Bouteille Exotique - 0.75L",
            ),
        ]
        # Brassin n'a que 33cl et 75cl Verralia/SAFT, pas "Exotique"
        result = resolve_bottle_stock(
            tenant_id="t",
            brassin_fils=_make_brassin_fils(),
            id_produit=42397,
            fmt="6x75",
            marque="EXOTIC",  # ni SYMBIOSE ni NIKO
        )
        # On peut quand même fallback sur "1 seul fil par contenance" → False ici
        # (2 fils 75cl), donc None attendu.
        assert result is None

    @patch("common.services.bottle_stock_resolver.run_sql")
    def test_ambiguous_two_sk_templates_returns_none(self, mock_sql: MagicMock):
        """2 templates SK-* (2 SKUs Symbiose 75cl) sans signal pour trancher → None."""
        mock_sql.return_value = [
            _make_template(
                code_article="SK-KDF-75-ORI",
                contenance=0.75, lot_quantite=6,
                contenant_libelle="Bouteille 75cl Verralia - 0.75L",
            ),
            _make_template(
                code_article="SK-KDF-75-GIN",
                contenance=0.75, lot_quantite=6,
                contenant_libelle="Bouteille 75cl Verralia - 0.75L",
            ),
        ]
        result = resolve_bottle_stock(
            tenant_id="t",
            brassin_fils=_make_brassin_fils(),
            id_produit=42397,
            fmt="6x75",
            marque="SYMBIOSE",
        )
        # 2 SK-* sans plus de signal → ambigu
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
