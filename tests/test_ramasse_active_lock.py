"""Tests du verrou « 1 ramasse active à la fois par destinataire ».

- ``has_active_ramasse_for_dest`` : check SQL des filtres (status,
  driver_passed, deleted_at, tenant scoping).
- ``get_active_ramasse_for_dest`` : retour None vs dict, JOIN users.
- ``save_ramasse`` : refuse l'INSERT en CREATE si une ramasse active
  existe (status previsionnel ou definitif).
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from common.ramasse_history import (
    get_active_ramasse_for_dest,
    has_active_ramasse_for_dest,
    save_ramasse,
)


class TestHasActiveRamasseForDest:

    @patch("common.ramasse_history.run_sql")
    def test_returns_true_when_row_exists(self, mock_run):
        mock_run.return_value = [{"?column?": 1}]
        assert has_active_ramasse_for_dest("Sofripa", tenant_id="t1") is True

    @patch("common.ramasse_history.run_sql")
    def test_returns_false_when_no_row(self, mock_run):
        mock_run.return_value = []
        assert has_active_ramasse_for_dest("Sofripa", tenant_id="t1") is False

    @patch("common.ramasse_history.run_sql")
    def test_sql_filters_only_open_active_ramasses(self, mock_run):
        # Garde-fou : les status legacy / sent ne doivent jamais bloquer,
        # tout comme les livrées ou les corbeillées.
        mock_run.return_value = []
        has_active_ramasse_for_dest("Sofripa", tenant_id="tid-7")
        sql = mock_run.call_args[0][0]
        params = mock_run.call_args[0][1]
        assert "status IN ('previsionnel', 'definitif')" in sql
        assert "driver_passed = FALSE" in sql
        assert "deleted_at IS NULL" in sql
        assert params == {"tid": "tid-7", "dest": "Sofripa"}


class TestGetActiveRamasseForDest:

    @patch("common.ramasse_history.run_sql")
    def test_returns_none_when_no_match(self, mock_run):
        mock_run.return_value = []
        assert get_active_ramasse_for_dest("Sofripa", tenant_id="t1") is None

    @patch("common.ramasse_history.run_sql")
    def test_returns_dict_with_creator_email(self, mock_run):
        mock_run.return_value = [{
            "id": "abc-123", "date_ramasse": date(2026, 5, 15),
            "destinataire": "Sofripa", "status": "previsionnel",
            "total_palettes": 8, "total_cartons": 96, "total_poids_kg": 2400,
            "version": 1, "driver_passed": False,
            "created_at": None, "updated_at": None,
            "created_by_email": "max@ferment.test",
        }]
        rec = get_active_ramasse_for_dest("Sofripa", tenant_id="t1")
        assert rec is not None
        assert rec["created_by_email"] == "max@ferment.test"
        assert rec["status"] == "previsionnel"

    @patch("common.ramasse_history.run_sql")
    def test_sql_joins_users_for_email(self, mock_run):
        # On veut « créée par X » dans le bandeau d'état — donc le JOIN
        # users doit être présent même si l'user a été supprimé (LEFT).
        mock_run.return_value = []
        get_active_ramasse_for_dest("Sofripa", tenant_id="t1")
        sql = mock_run.call_args[0][0]
        assert "LEFT JOIN users u ON u.id = rh.created_by" in sql
        assert "u.email AS created_by_email" in sql


class TestSaveRamasseLock:

    @patch("common.ramasse_history.current_tenant_id", return_value="t1")
    @patch("common.ramasse_history.current_user_id", return_value="u1")
    @patch("common.ramasse_history.has_active_ramasse_for_dest")
    @patch("common.ramasse_history.run_sql")
    def test_refuses_create_previsionnel_if_active_exists(
        self, mock_run, mock_has, _u, _t,
    ):
        mock_has.return_value = True  # une ramasse est déjà active
        with pytest.raises(ValueError, match="déjà en cours"):
            save_ramasse(
                date_ramasse=date(2026, 5, 15),
                destinataire="Sofripa",
                recipients=["a@b.com"],
                lines=[], total_cartons=0, total_palettes=0, total_poids_kg=0,
                status="previsionnel",
            )
        # L'INSERT ne doit pas avoir été lancé
        mock_run.assert_not_called()

    @patch("common.ramasse_history.current_tenant_id", return_value="t1")
    @patch("common.ramasse_history.current_user_id", return_value="u1")
    @patch("common.ramasse_history.has_active_ramasse_for_dest")
    @patch("common.ramasse_history.run_sql")
    def test_refuses_create_definitif_if_active_exists(
        self, mock_run, mock_has, _u, _t,
    ):
        mock_has.return_value = True
        with pytest.raises(ValueError, match="déjà en cours"):
            save_ramasse(
                date_ramasse=date(2026, 5, 15),
                destinataire="Sofripa",
                recipients=["a@b.com"],
                lines=[], total_cartons=0, total_palettes=0, total_poids_kg=0,
                status="definitif",
            )
        mock_run.assert_not_called()

    @patch("common.ramasse_history.current_tenant_id", return_value="t1")
    @patch("common.ramasse_history.current_user_id", return_value="u1")
    @patch("common.ramasse_history._audit")
    @patch("common.ramasse_history.has_active_ramasse_for_dest")
    @patch("common.ramasse_history.run_sql")
    def test_allows_create_when_no_active(
        self, mock_run, mock_has, _audit, _u, _t,
    ):
        mock_has.return_value = False
        mock_run.return_value = [{"id": "new-id"}]
        rid = save_ramasse(
            date_ramasse=date(2026, 5, 15),
            destinataire="Sofripa",
            recipients=["a@b.com"],
            lines=[], total_cartons=0, total_palettes=0, total_poids_kg=0,
            status="previsionnel",
        )
        assert rid == "new-id"
        mock_run.assert_called_once()

    @patch("common.ramasse_history.current_tenant_id", return_value="t1")
    @patch("common.ramasse_history.current_user_id", return_value="u1")
    @patch("common.ramasse_history._audit")
    @patch("common.ramasse_history.has_active_ramasse_for_dest")
    @patch("common.ramasse_history.run_sql")
    def test_legacy_status_bypasses_lock(
        self, mock_run, mock_has, _audit, _u, _t,
    ):
        # Garde-fou de cohérence : un import legacy / migration ne doit
        # pas être bloqué par le verrou (status != previsionnel/definitif).
        mock_run.return_value = [{"id": "legacy-id"}]
        save_ramasse(
            date_ramasse=date(2026, 5, 15),
            destinataire="Sofripa",
            recipients=["a@b.com"],
            lines=[], total_cartons=0, total_palettes=0, total_poids_kg=0,
            status="legacy",
        )
        # has_active_ramasse_for_dest ne doit même pas être appelé
        mock_has.assert_not_called()
        mock_run.assert_called_once()
