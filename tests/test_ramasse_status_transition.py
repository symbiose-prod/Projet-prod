"""Tests de la machine de transition de statut sur update_ramasse.

Vérifie le garde-fou anti-régression : un BL ``definitif`` ne doit pas
pouvoir redevenir ``previsionnel`` via une mise à jour ultérieure. Le
chauffeur s'est basé sur le définitif — toute modification de ses lignes
doit rester ``definitif`` (correction).

Mocke run_sql / get_ramasse — pas de DB réelle.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from common.ramasse_history import (
    _VALID_STATUS_TRANSITIONS,
    update_ramasse,
)


def _make_existing(status: str, *, driver_passed: bool = False) -> dict:
    """Forge un record ramasse_history minimal pour les tests."""
    return {
        "id": "abc-123",
        "lines": [],
        "version": 1,
        "version_log": [],
        "line_count": 0,
        "total_cartons": 0,
        "total_palettes": 0,
        "total_poids_kg": 0,
        "status": status,
        "driver_passed": driver_passed,
        "packaging": [],
    }


class TestValidTransitionsTable:
    """Garde-fous sur la table de transitions elle-même — invariants métier."""

    def test_definitif_cannot_regress_to_previsionnel(self):
        # Le cas central : une fois définitif, on n'admet plus de
        # bascule en previsionnel.
        assert "previsionnel" not in _VALID_STATUS_TRANSITIONS["definitif"]

    def test_definitif_can_be_corrected(self):
        # Mais on doit pouvoir corriger un définitif (par ex. retirer
        # une palette qui n'est finalement pas partie).
        assert "definitif" in _VALID_STATUS_TRANSITIONS["definitif"]

    def test_previsionnel_can_become_definitif(self):
        # Transition canonique au moment du chargement.
        assert "definitif" in _VALID_STATUS_TRANSITIONS["previsionnel"]

    def test_previsionnel_can_be_resent(self):
        # Option 1 validée avec l'utilisateur : autant de prévisionnels
        # qu'on veut avant le définitif.
        assert "previsionnel" in _VALID_STATUS_TRANSITIONS["previsionnel"]

    def test_legacy_is_quarantined(self):
        # Une ramasse legacy (créée avant la refonte) ne peut pas être
        # promue dans le nouveau workflow — on l'isole pour éviter les
        # confusions sur des données saisies à la main.
        assert _VALID_STATUS_TRANSITIONS["legacy"] == {"legacy"}


class TestUpdateRamasseStatusTransition:

    @patch("common.ramasse_history.run_sql")
    @patch("common.ramasse_history.get_ramasse")
    def test_previsionnel_to_definitif_allowed(self, mock_get, mock_run):
        mock_get.return_value = _make_existing("previsionnel")
        mock_run.return_value = [{"id": "abc", "version": 2, "updated_at": None}]
        result = update_ramasse(
            "abc-123",
            date_ramasse=date(2026, 5, 14),
            destinataire="Sofripa", recipients=["a@b.com"],
            lines=[], total_cartons=0, total_palettes=0, total_poids_kg=0,
            target_status="definitif", tenant_id="t1",
        )
        assert result is not None
        # La query d'UPDATE doit inclure le set status = :status
        sql = mock_run.call_args[0][0]
        params = mock_run.call_args[0][1]
        assert "status = :status" in sql
        assert params["status"] == "definitif"

    @patch("common.ramasse_history.run_sql")
    @patch("common.ramasse_history.get_ramasse")
    def test_definitif_to_previsionnel_refused(self, mock_get, mock_run):
        mock_get.return_value = _make_existing("definitif")
        result = update_ramasse(
            "abc-123",
            date_ramasse=date(2026, 5, 14),
            destinataire="Sofripa", recipients=["a@b.com"],
            lines=[], total_cartons=0, total_palettes=0, total_poids_kg=0,
            target_status="previsionnel", tenant_id="t1",
        )
        # Régression refusée → None et aucun UPDATE émis
        assert result is None
        mock_run.assert_not_called()

    @patch("common.ramasse_history.run_sql")
    @patch("common.ramasse_history.get_ramasse")
    def test_definitif_to_definitif_allowed(self, mock_get, mock_run):
        # Correction d'un BL définitif (rare mais autorisé)
        mock_get.return_value = _make_existing("definitif")
        mock_run.return_value = [{"id": "abc", "version": 2, "updated_at": None}]
        result = update_ramasse(
            "abc-123",
            date_ramasse=date(2026, 5, 14),
            destinataire="Sofripa", recipients=["a@b.com"],
            lines=[], total_cartons=0, total_palettes=0, total_poids_kg=0,
            target_status="definitif", tenant_id="t1",
        )
        assert result is not None

    @patch("common.ramasse_history.run_sql")
    @patch("common.ramasse_history.get_ramasse")
    def test_previsionnel_to_previsionnel_allowed(self, mock_get, mock_run):
        # Option 1 : autant de prévisionnels qu'on veut
        mock_get.return_value = _make_existing("previsionnel")
        mock_run.return_value = [{"id": "abc", "version": 3, "updated_at": None}]
        result = update_ramasse(
            "abc-123",
            date_ramasse=date(2026, 5, 14),
            destinataire="Sofripa", recipients=["a@b.com"],
            lines=[], total_cartons=0, total_palettes=0, total_poids_kg=0,
            target_status="previsionnel", tenant_id="t1",
        )
        assert result is not None

    @patch("common.ramasse_history.run_sql")
    @patch("common.ramasse_history.get_ramasse")
    def test_target_status_none_leaves_status_alone(self, mock_get, mock_run):
        # Backward-compat : si target_status n'est pas fourni, on ne
        # touche pas au status (la query d'UPDATE ne contient pas la
        # colonne status).
        mock_get.return_value = _make_existing("previsionnel")
        mock_run.return_value = [{"id": "abc", "version": 2, "updated_at": None}]
        update_ramasse(
            "abc-123",
            date_ramasse=date(2026, 5, 14),
            destinataire="Sofripa", recipients=["a@b.com"],
            lines=[], total_cartons=0, total_palettes=0, total_poids_kg=0,
            tenant_id="t1",
        )
        sql = mock_run.call_args[0][0]
        assert "status = :status" not in sql

    @patch("common.ramasse_history.run_sql")
    @patch("common.ramasse_history.get_ramasse")
    def test_driver_passed_blocks_update_regardless_of_status(self, mock_get, mock_run):
        # Garde-fou existant prioritaire sur la transition : si le
        # chauffeur est passé, on ne touche plus, même pour rester en
        # définitif.
        mock_get.return_value = _make_existing("definitif", driver_passed=True)
        result = update_ramasse(
            "abc-123",
            date_ramasse=date(2026, 5, 14),
            destinataire="Sofripa", recipients=["a@b.com"],
            lines=[], total_cartons=0, total_palettes=0, total_poids_kg=0,
            target_status="definitif", tenant_id="t1",
        )
        assert result is None
        mock_run.assert_not_called()

    @patch("common.ramasse_history.run_sql")
    @patch("common.ramasse_history.get_ramasse")
    def test_legacy_to_previsionnel_refused(self, mock_get, mock_run):
        # Une ramasse legacy ne peut pas être promue — on évite que les
        # anciennes saisies manuelles polluent le nouveau workflow.
        mock_get.return_value = _make_existing("legacy")
        result = update_ramasse(
            "abc-123",
            date_ramasse=date(2026, 5, 14),
            destinataire="Sofripa", recipients=["a@b.com"],
            lines=[], total_cartons=0, total_palettes=0, total_poids_kg=0,
            target_status="previsionnel", tenant_id="t1",
        )
        assert result is None
        mock_run.assert_not_called()
