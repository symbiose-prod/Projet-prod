"""Tests des fonctions de common.services.production_sheet_service.

Mock SQL via run_sql — on teste la logique de transformation, pas la DB.
"""
from __future__ import annotations

from unittest import mock

from common.services import production_sheet_service
from common.services.production_sheet_service import (
    ConditionnementByLot,
    ConditionnementLine,
    compute_real_conditionnement_by_lot,
)

# ─── compute_real_conditionnement_by_lot ───────────────────────────────────

class TestComputeRealConditionnementByLot:

    def test_empty_lot_returns_empty_without_query(self):
        # On ne doit même pas appeler run_sql si le lot est vide
        with mock.patch.object(
            production_sheet_service, "run_sql",
        ) as mock_sql:
            result = compute_real_conditionnement_by_lot("tenant-A", "")
        mock_sql.assert_not_called()
        assert result.lot == ""
        assert result.items == []
        assert result.total_cartons == 0
        assert result.total_palettes == 0

    def test_whitespace_only_lot_returns_empty(self):
        with mock.patch.object(
            production_sheet_service, "run_sql",
        ) as mock_sql:
            result = compute_real_conditionnement_by_lot("tenant-A", "   ")
        mock_sql.assert_not_called()
        assert result.items == []

    def test_aggregates_rows_into_lines(self):
        # 2 lignes agrégées par (fmt, marque) en sortie SQL
        fake_rows = [
            {
                "fmt": "12x33", "marque": "SYMBIOSE",
                "designation": "K. Mangue - Passion",
                "total_cartons": 843, "total_palettes": 12,
            },
            {
                "fmt": "6x75", "marque": "SYMBIOSE",
                "designation": "K. Mangue - Passion",
                "total_cartons": 347, "total_palettes": 4,
            },
        ]
        with mock.patch.object(
            production_sheet_service, "run_sql", return_value=fake_rows,
        ) as mock_sql:
            result = compute_real_conditionnement_by_lot("tenant-A", "15052027")
        # SQL appelé avec les bons params
        assert mock_sql.call_args[0][1] == {
            "tid": "tenant-A", "lot": "15052027",
        }
        # Output
        assert isinstance(result, ConditionnementByLot)
        assert result.lot == "15052027"
        assert len(result.items) == 2
        assert result.items[0] == ConditionnementLine(
            fmt="12x33", marque="SYMBIOSE",
            designation="K. Mangue - Passion",
            cartons=843, palettes=12,
        )
        assert result.total_cartons == 843 + 347
        assert result.total_palettes == 12 + 4

    def test_no_rows_returns_zero_totals(self):
        # Lot inconnu / aucune palette étiquetée → totaux à 0
        with mock.patch.object(
            production_sheet_service, "run_sql", return_value=[],
        ):
            result = compute_real_conditionnement_by_lot("tenant-A", "ZZZ")
        assert result.items == []
        assert result.total_cartons == 0
        assert result.total_palettes == 0

    def test_handles_null_values_defensively(self):
        # case_count ou marque NULL dans la DB → on coerce sans crasher
        fake_rows = [
            {
                "fmt": None, "marque": None, "designation": None,
                "total_cartons": None, "total_palettes": 3,
            },
        ]
        with mock.patch.object(
            production_sheet_service, "run_sql", return_value=fake_rows,
        ):
            result = compute_real_conditionnement_by_lot("tenant-A", "L")
        assert result.items[0].fmt == ""
        assert result.items[0].marque == ""
        assert result.items[0].cartons == 0
        assert result.items[0].palettes == 3

    def test_lot_is_trimmed_before_query(self):
        with mock.patch.object(
            production_sheet_service, "run_sql", return_value=[],
        ) as mock_sql:
            compute_real_conditionnement_by_lot("tenant-A", "  15052027  ")
        # Le lot transmis à SQL est trimmé
        assert mock_sql.call_args[0][1]["lot"] == "15052027"
