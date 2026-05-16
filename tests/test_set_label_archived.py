"""
tests/test_set_label_archived.py
=================================
Tests pour ``etiquette_palette_service.set_label_archived`` — toggle de
l'état archivé d'une étiquette palette historisée.

Couvre :
  - toggle (archived=None) : flip atomique via SQL CASE
  - force True / False : assignation explicite
  - sécurité multi-tenant : un label d'un autre tenant retourne False
  - erreur DB : retourne False, ne propage pas
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import MagicMock, patch

from common.services.etiquette_palette_service import set_label_archived


class TestSetLabelArchivedToggle:
    """Mode toggle (archived=None) — flip l'état actuel."""

    @patch("common.services.etiquette_palette_service.run_sql")
    def test_toggle_returns_datetime_when_archived(self, mock_sql: MagicMock):
        # La query retourne la nouvelle valeur de archived_at (= now())
        now = _dt.datetime.now(_dt.UTC)
        mock_sql.return_value = [{"archived_at": now}]
        result = set_label_archived("tenant-A", 42, archived=None)
        assert result == now

    @patch("common.services.etiquette_palette_service.run_sql")
    def test_toggle_returns_none_when_unarchived(self, mock_sql: MagicMock):
        # La query retourne archived_at = None (a été désarchivée)
        mock_sql.return_value = [{"archived_at": None}]
        result = set_label_archived("tenant-A", 42, archived=None)
        assert result is None

    @patch("common.services.etiquette_palette_service.run_sql")
    def test_toggle_uses_case_when_in_sql(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"archived_at": None}]
        set_label_archived("tenant-A", 42, archived=None)
        sql_arg = mock_sql.call_args[0][0]
        # Toggle = `CASE WHEN archived_at IS NULL THEN now() ELSE NULL END`
        assert "CASE WHEN archived_at IS NULL" in sql_arg


class TestSetLabelArchivedForce:
    """Mode force (archived=True/False) — assignation explicite."""

    @patch("common.services.etiquette_palette_service.run_sql")
    def test_force_true_passes_archived_param(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"archived_at": _dt.datetime.now(_dt.UTC)}]
        set_label_archived("tenant-A", 42, archived=True)
        params = mock_sql.call_args[0][1]
        assert params["a"] is True

    @patch("common.services.etiquette_palette_service.run_sql")
    def test_force_false_passes_archived_param(self, mock_sql: MagicMock):
        mock_sql.return_value = [{"archived_at": None}]
        set_label_archived("tenant-A", 42, archived=False)
        params = mock_sql.call_args[0][1]
        assert params["a"] is False


class TestSetLabelArchivedTenantIsolation:
    """Sécurité multi-tenant : un label d'un autre tenant doit retourner False."""

    @patch("common.services.etiquette_palette_service.run_sql")
    def test_returns_false_if_label_not_found_in_tenant(self, mock_sql: MagicMock):
        # Label inexistant ou appartenant à un autre tenant → UPDATE retourne 0 rows
        mock_sql.return_value = []
        result = set_label_archived("tenant-A", 99999, archived=True)
        assert result is False

    @patch("common.services.etiquette_palette_service.run_sql")
    def test_sql_includes_tenant_id_filter(self, mock_sql: MagicMock):
        mock_sql.return_value = []
        set_label_archived("tenant-A", 42, archived=None)
        # Vérification critique : la query DOIT filtrer par tenant_id
        sql_arg = mock_sql.call_args[0][0]
        assert "tenant_id = :t" in sql_arg
        params = mock_sql.call_args[0][1]
        assert params["t"] == "tenant-A"


class TestSetLabelArchivedErrors:
    """Robustesse : erreur DB ne propage pas — retourne False."""

    @patch("common.services.etiquette_palette_service.run_sql")
    def test_db_error_returns_false(self, mock_sql: MagicMock):
        mock_sql.side_effect = RuntimeError("Postgres unreachable")
        result = set_label_archived("tenant-A", 42, archived=True)
        assert result is False
