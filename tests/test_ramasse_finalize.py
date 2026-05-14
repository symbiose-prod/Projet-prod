"""Tests unitaires pour finalize_ramasse_lines de common/ramasse_history.

``finalize_ramasse_lines`` patche les lignes d'une ramasse fraîchement
créée en placeholder, sans toucher à version/version_log/previous_lines.
Sémantique différente de ``update_ramasse`` (qui versionne).

Mock de ``run_sql`` — pas de DB réelle.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from common.ramasse_history import finalize_ramasse_lines


class TestFinalizeRamasseLines:

    @patch("common.ramasse_history.run_sql")
    def test_returns_true_when_row_updated(self, mock_run_sql):
        mock_run_sql.return_value = [{"id": "abc-123"}]
        ok = finalize_ramasse_lines(
            "abc-123",
            lines=[{"ref": "X", "cartons": 10}],
            total_cartons=10, total_palettes=1, total_poids_kg=100,
            tenant_id="t1",
        )
        assert ok is True

    @patch("common.ramasse_history.run_sql")
    def test_returns_false_when_no_row_matched(self, mock_run_sql):
        mock_run_sql.return_value = []
        ok = finalize_ramasse_lines(
            "missing-id", lines=[], total_cartons=0,
            total_palettes=0, total_poids_kg=0, tenant_id="t1",
        )
        assert ok is False

    @patch("common.ramasse_history.run_sql")
    def test_does_not_touch_version_columns(self, mock_run_sql):
        # Garde-fou de régression : finalize ne doit PAS toucher version /
        # version_log / previous_lines. Ces colonnes sont réservées à
        # update_ramasse qui versionne explicitement.
        mock_run_sql.return_value = [{"id": "abc-123"}]
        finalize_ramasse_lines(
            "abc-123",
            lines=[], total_cartons=0,
            total_palettes=0, total_poids_kg=0, tenant_id="t1",
        )
        sql = mock_run_sql.call_args[0][0]
        assert "version" not in sql.lower()
        assert "previous_lines" not in sql

    @patch("common.ramasse_history.run_sql")
    def test_persists_lines_as_jsonb(self, mock_run_sql):
        mock_run_sql.return_value = [{"id": "abc-123"}]
        my_lines = [
            {"ref": "A", "cartons": 10, "palettes": 1, "poids": 100},
            {"ref": "B", "cartons": 20, "palettes": 2, "poids": 200},
        ]
        finalize_ramasse_lines(
            "abc-123",
            lines=my_lines,
            total_cartons=30, total_palettes=3, total_poids_kg=300,
            tenant_id="t1",
        )
        params = mock_run_sql.call_args[0][1]
        # line_count dérivé de len(lines), totaux passés tels quels
        assert params["lc"] == 2
        assert params["tc"] == 30
        assert params["tp"] == 3
        assert params["tpk"] == 300
        # lines sérialisées en JSON
        assert json.loads(params["lines"]) == my_lines

    @patch("common.ramasse_history.run_sql")
    def test_pdf_bytes_optional_uses_coalesce(self, mock_run_sql):
        # Sans pdf_bytes : la query doit garder le PDF existant (COALESCE).
        mock_run_sql.return_value = [{"id": "abc-123"}]
        finalize_ramasse_lines(
            "abc-123",
            lines=[], total_cartons=0,
            total_palettes=0, total_poids_kg=0, tenant_id="t1",
        )
        sql = mock_run_sql.call_args[0][0]
        assert "COALESCE(:pdf, pdf_bytes)" in sql

    @patch("common.ramasse_history.run_sql")
    def test_packaging_omitted_keeps_existing(self, mock_run_sql):
        # Sans packaging : la query ne doit pas écraser le packaging
        # existant. La colonne packaging n'apparaît PAS dans le SET.
        mock_run_sql.return_value = [{"id": "abc-123"}]
        finalize_ramasse_lines(
            "abc-123",
            lines=[], total_cartons=0,
            total_palettes=0, total_poids_kg=0, tenant_id="t1",
        )
        sql = mock_run_sql.call_args[0][0]
        assert "packaging" not in sql

    @patch("common.ramasse_history.run_sql")
    def test_packaging_provided_is_persisted(self, mock_run_sql):
        mock_run_sql.return_value = [{"id": "abc-123"}]
        pkg = [{"label": "Bouteilles vides", "qty": 5, "unit": "palette"}]
        finalize_ramasse_lines(
            "abc-123",
            lines=[], total_cartons=0,
            total_palettes=0, total_poids_kg=0,
            packaging=pkg,
            tenant_id="t1",
        )
        sql = mock_run_sql.call_args[0][0]
        params = mock_run_sql.call_args[0][1]
        assert "packaging = CAST(:pkg AS jsonb)" in sql
        assert json.loads(params["pkg"]) == pkg

    @patch("common.ramasse_history.run_sql")
    def test_scoped_by_tenant(self, mock_run_sql):
        # Sécurité : on doit toujours filtrer par tenant_id en plus de l'id
        mock_run_sql.return_value = [{"id": "abc-123"}]
        finalize_ramasse_lines(
            "abc-123",
            lines=[], total_cartons=0,
            total_palettes=0, total_poids_kg=0,
            tenant_id="tenant-42",
        )
        sql = mock_run_sql.call_args[0][0]
        params = mock_run_sql.call_args[0][1]
        assert "WHERE id = :rid AND tenant_id = :tid" in sql
        assert params["tid"] == "tenant-42"
        assert params["rid"] == "abc-123"
