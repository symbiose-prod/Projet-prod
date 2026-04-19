"""Tests unitaires pour delete/restore/purge de common/ramasse_history.

Mock de ``run_sql`` — pas de DB réelle.
"""
from __future__ import annotations

from unittest.mock import patch

from common.ramasse_history import (
    delete_ramasse,
    purge_expired_ramasses,
    restore_ramasse,
)


class TestDeleteRamasse:
    @patch("common.ramasse_history._audit")
    @patch("common.ramasse_history.run_sql")
    def test_soft_delete_sets_deleted_at(self, mock_run_sql, mock_audit):
        mock_run_sql.return_value = [{"id": "abc-123"}]
        ok = delete_ramasse("abc-123", tenant_id="t1")
        assert ok is True
        sql, params = mock_run_sql.call_args[0]
        assert "UPDATE ramasse_history" in sql
        assert "SET deleted_at = now()" in sql
        # Protection : ne touche pas aux ramasses déjà supprimées (idempotence)
        assert "deleted_at IS NULL" in sql
        assert params == {"rid": "abc-123", "tid": "t1"}
        mock_audit.assert_called_once()
        action_arg = mock_audit.call_args[0][0]
        assert action_arg == "ramasse_deleted"
        details = mock_audit.call_args[0][2]
        assert details == {"ramasse_id": "abc-123", "soft": True}

    @patch("common.ramasse_history._audit")
    @patch("common.ramasse_history.run_sql")
    def test_delete_returns_false_if_already_deleted(self, mock_run_sql, mock_audit):
        mock_run_sql.return_value = []  # WHERE deleted_at IS NULL ne matche pas
        ok = delete_ramasse("abc-123", tenant_id="t1")
        assert ok is False
        mock_audit.assert_not_called()

    @patch("common.ramasse_history._audit")
    @patch("common.ramasse_history.run_sql")
    def test_delete_returns_false_if_not_found(self, mock_run_sql, mock_audit):
        mock_run_sql.return_value = []
        ok = delete_ramasse("missing-id", tenant_id="t1")
        assert ok is False
        mock_audit.assert_not_called()


class TestRestoreRamasse:
    @patch("common.ramasse_history._audit")
    @patch("common.ramasse_history.run_sql")
    def test_restore_resets_deleted_at(self, mock_run_sql, mock_audit):
        mock_run_sql.return_value = [{"id": "abc-123"}]
        ok = restore_ramasse("abc-123", tenant_id="t1")
        assert ok is True
        sql, params = mock_run_sql.call_args[0]
        assert "UPDATE ramasse_history" in sql
        assert "SET deleted_at = NULL" in sql
        assert "deleted_at IS NOT NULL" in sql  # ne restaure que les supprimées
        assert params == {"rid": "abc-123", "tid": "t1"}
        action_arg = mock_audit.call_args[0][0]
        assert action_arg == "ramasse_restored"

    @patch("common.ramasse_history._audit")
    @patch("common.ramasse_history.run_sql")
    def test_restore_fails_if_not_deleted(self, mock_run_sql, mock_audit):
        mock_run_sql.return_value = []
        ok = restore_ramasse("abc-123", tenant_id="t1")
        assert ok is False
        mock_audit.assert_not_called()


class TestPurgeExpired:
    @patch("common.ramasse_history.run_sql")
    def test_purge_returns_count(self, mock_run_sql):
        mock_run_sql.return_value = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        n = purge_expired_ramasses(retention_days=7)
        assert n == 3
        sql, params = mock_run_sql.call_args[0]
        assert "DELETE FROM ramasse_history" in sql
        assert "deleted_at IS NOT NULL" in sql
        assert "make_interval(days => :days)" in sql
        assert params["days"] == 7
        # Sans tenant_id, pas de filtre tenant
        assert "tid" not in params

    @patch("common.ramasse_history.run_sql")
    def test_purge_with_tenant_scope(self, mock_run_sql):
        mock_run_sql.return_value = [{"id": "a"}]
        n = purge_expired_ramasses(retention_days=30, tenant_id="t42")
        assert n == 1
        sql, params = mock_run_sql.call_args[0]
        assert "tenant_id = :tid" in sql
        assert params["tid"] == "t42"
        assert params["days"] == 30

    @patch("common.ramasse_history.run_sql")
    def test_purge_empty_returns_zero(self, mock_run_sql):
        mock_run_sql.return_value = []
        n = purge_expired_ramasses()
        assert n == 0
