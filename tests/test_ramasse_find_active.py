"""Tests pour find_active_previsionnel_for_dest.

Pas de filtre temporel : un prévisionnel reste candidat tant qu'il n'est
pas converti en définitif ou marqué livré. Le test vérifie surtout les
filtres SQL (status, driver_passed, deleted_at) et le tenant scoping.
"""
from __future__ import annotations

from unittest.mock import patch

from common.ramasse_history import find_active_previsionnel_for_dest


class TestFindActivePrevisionnelForDest:

    @patch("common.ramasse_history.run_sql")
    def test_returns_id_when_match(self, mock_run):
        mock_run.return_value = [{"id": "abc-123"}]
        result = find_active_previsionnel_for_dest("Sofripa", tenant_id="t1")
        assert result == "abc-123"

    @patch("common.ramasse_history.run_sql")
    def test_returns_none_when_no_match(self, mock_run):
        mock_run.return_value = []
        result = find_active_previsionnel_for_dest("Sofripa", tenant_id="t1")
        assert result is None

    @patch("common.ramasse_history.run_sql")
    def test_sql_filters_status_and_open_state(self, mock_run):
        # Garde-fou : on ne doit jamais ramener un définitif, un legacy,
        # une ramasse livrée ou dans la corbeille.
        mock_run.return_value = []
        find_active_previsionnel_for_dest("Sofripa", tenant_id="tid-7")
        sql = mock_run.call_args[0][0]
        params = mock_run.call_args[0][1]
        assert "status       = 'previsionnel'" in sql
        assert "driver_passed = FALSE" in sql
        assert "deleted_at  IS NULL" in sql
        assert "ORDER BY created_at DESC" in sql
        assert "LIMIT 1" in sql
        assert params == {"tid": "tid-7", "dest": "Sofripa"}

    @patch("common.ramasse_history.run_sql")
    def test_scoped_by_tenant(self, mock_run):
        # Garde-fou multi-tenant : un prévisionnel d'un autre tenant ne
        # doit jamais remonter même si destinataire match.
        mock_run.return_value = []
        find_active_previsionnel_for_dest("Sofripa", tenant_id="my-tenant")
        sql = mock_run.call_args[0][0]
        params = mock_run.call_args[0][1]
        assert "tenant_id    = :tid" in sql
        assert params["tid"] == "my-tenant"
