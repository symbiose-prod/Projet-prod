"""Tests for common/storage.py — production proposal storage (mocked run_sql)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from common.storage import (
    _decode_sp,
    _encode_sp,
    delete_snapshot,
    list_saved,
    load_snapshot,
    rename_snapshot,
    save_snapshot,
)

# ─── Encoding/Decoding ──────────────────────────────────────────────────────


class TestEncodeDecode:
    def test_roundtrip_dict(self):
        sp = {
            "semaine_du": "2026-03-01",
            "ddm": "2026-06-01",
            "gouts": ["Original", "Citron"],
            "df_min": pd.DataFrame({"a": [1, 2]}),
            "df_calc": pd.DataFrame({"b": [3, 4]}),
        }
        encoded = _encode_sp(sp)
        assert isinstance(encoded["df_min"], str)
        assert isinstance(encoded["df_calc"], str)
        assert encoded["gouts"] == ["Original", "Citron"]

        decoded = _decode_sp(encoded)
        assert decoded["semaine_du"] == "2026-03-01"
        assert decoded["gouts"] == ["Original", "Citron"]
        assert isinstance(decoded["df_min"], pd.DataFrame)
        assert len(decoded["df_min"]) == 2

    def test_encode_no_dataframes(self):
        sp = {"semaine_du": "2026-03-01", "gouts": ["A"]}
        encoded = _encode_sp(sp)
        assert encoded["df_min"] is None
        assert encoded["df_calc"] is None

    def test_decode_no_dataframes(self):
        obj = {"semaine_du": "2026-03-01", "gouts": ["A"], "df_min": None, "df_calc": None}
        decoded = _decode_sp(obj)
        assert decoded["df_min"] is None
        assert decoded["df_calc"] is None

    def test_decode_empty_string(self):
        obj = {"semaine_du": "2026-03-01", "gouts": [], "df_min": "", "df_calc": "  "}
        decoded = _decode_sp(obj)
        assert decoded["df_min"] is None
        assert decoded["df_calc"] is None


# ─── list_saved ──────────────────────────────────────────────────────────────


class TestListSaved:
    @patch("common.storage._tenant_id", return_value="t1")
    @patch("common.storage.run_sql")
    def test_returns_sorted(self, mock_sql: MagicMock, mock_tid: MagicMock):
        mock_sql.return_value = [
            {"id": "p1", "created_at": "2026-03-01", "updated_at": None, "payload": {
                "_meta": {"name": "Plan A", "ts": "2026-03-01T10:00:00Z"},
                "gouts": ["Original"], "semaine_du": "2026-03-01",
            }},
            {"id": "p2", "created_at": "2026-03-02", "updated_at": None, "payload": {
                "_meta": {"name": "Plan B", "ts": "2026-03-02T10:00:00Z"},
                "gouts": ["Citron"], "semaine_du": "2026-03-02",
            }},
        ]
        result = list_saved()
        assert len(result) == 2
        assert result[0]["name"] == "Plan B"  # most recent first
        assert result[1]["name"] == "Plan A"

    @patch("common.storage._tenant_id", return_value="t1")
    @patch("common.storage.run_sql")
    def test_empty(self, mock_sql: MagicMock, mock_tid: MagicMock):
        mock_sql.return_value = []
        assert list_saved() == []


# ─── save_snapshot ───────────────────────────────────────────────────────────


class TestSaveSnapshot:
    def test_empty_name(self):
        ok, msg = save_snapshot("", {"gouts": []})
        assert ok is False
        assert "Nom vide" in msg

    @patch("common.storage._system_user_id", return_value="sys1")
    @patch("common.storage._tenant_id", return_value="t1")
    @patch("common.storage.run_sql")
    def test_update_existing(self, mock_sql: MagicMock, mock_tid: MagicMock, mock_uid: MagicMock):
        # First call: find existing
        # Second call: update
        mock_sql.side_effect = [
            [{"id": "p1"}],  # SELECT existing
            1,  # UPDATE
        ]
        ok, msg = save_snapshot("Plan A", {"gouts": ["A"]})
        assert ok is True
        assert "mise à jour" in msg

    @patch("common.storage._system_user_id", return_value="sys1")
    @patch("common.storage._tenant_id", return_value="t1")
    @patch("common.storage.run_sql")
    def test_insert_new(self, mock_sql: MagicMock, mock_tid: MagicMock, mock_uid: MagicMock):
        mock_sql.side_effect = [
            [],  # SELECT existing: not found
            [{"id": "p1"}],  # INSERT RETURNING
        ]
        ok, msg = save_snapshot("Plan B", {"gouts": ["B"]})
        assert ok is True
        assert "enregistrée" in msg

    @patch("common.storage._system_user_id", return_value="sys1")
    @patch("common.storage._tenant_id", return_value="t1")
    @patch("common.storage.run_sql")
    def test_max_slots_reached(self, mock_sql: MagicMock, mock_tid: MagicMock, mock_uid: MagicMock):
        mock_sql.side_effect = [
            [],  # SELECT existing: not found
            [],  # INSERT returns nothing (limit reached)
        ]
        ok, msg = save_snapshot("Plan C", {"gouts": ["C"]})
        assert ok is False
        assert "Limite" in msg


# ─── load_snapshot ───────────────────────────────────────────────────────────


class TestLoadSnapshot:
    @patch("common.storage._tenant_id", return_value="t1")
    @patch("common.storage.run_sql")
    def test_found(self, mock_sql: MagicMock, mock_tid: MagicMock):
        mock_sql.return_value = [{"payload": {
            "semaine_du": "2026-03-01", "gouts": ["A"], "df_min": None, "df_calc": None,
        }}]
        result = load_snapshot("Plan A")
        assert result is not None
        assert result["semaine_du"] == "2026-03-01"

    @patch("common.storage._tenant_id", return_value="t1")
    @patch("common.storage.run_sql")
    def test_not_found(self, mock_sql: MagicMock, mock_tid: MagicMock):
        mock_sql.return_value = []
        assert load_snapshot("Missing") is None


# ─── delete_snapshot ─────────────────────────────────────────────────────────


class TestDeleteSnapshot:
    @patch("common.storage._tenant_id", return_value="t1")
    @patch("common.storage.run_sql")
    def test_delete_existing(self, mock_sql: MagicMock, mock_tid: MagicMock):
        mock_sql.return_value = [{"id": "p1"}]
        assert delete_snapshot("Plan A") is True

    @patch("common.storage._tenant_id", return_value="t1")
    @patch("common.storage.run_sql")
    def test_delete_missing(self, mock_sql: MagicMock, mock_tid: MagicMock):
        mock_sql.return_value = []
        assert delete_snapshot("Missing") is False


# ─── rename_snapshot ─────────────────────────────────────────────────────────


class TestRenameSnapshot:
    def test_empty_new_name(self):
        ok, msg = rename_snapshot("old", "")
        assert ok is False
        assert "Nouveau nom vide" in msg

    def test_same_name(self):
        ok, msg = rename_snapshot("Plan A", "Plan A")
        assert ok is True
        assert "Aucun changement" in msg

    @patch("common.storage._tenant_id", return_value="t1")
    @patch("common.storage.run_sql")
    def test_rename_success(self, mock_sql: MagicMock, mock_tid: MagicMock):
        mock_sql.return_value = [{"id": "p1"}]
        ok, msg = rename_snapshot("Old", "New")
        assert ok is True
        assert "Renommee" in msg

    @patch("common.storage._tenant_id", return_value="t1")
    @patch("common.storage.run_sql")
    def test_rename_conflict(self, mock_sql: MagicMock, mock_tid: MagicMock):
        mock_sql.side_effect = [
            [],  # UPDATE returned nothing
            [{"x": 1}],  # EXISTS check → name already taken
        ]
        ok, msg = rename_snapshot("Old", "Existing")
        assert ok is False
        assert "existe deja" in msg

    @patch("common.storage._tenant_id", return_value="t1")
    @patch("common.storage.run_sql")
    def test_rename_not_found(self, mock_sql: MagicMock, mock_tid: MagicMock):
        mock_sql.side_effect = [
            [],  # UPDATE returned nothing
            [],  # EXISTS check → no conflict
        ]
        ok, msg = rename_snapshot("Old", "New")
        assert ok is False
        assert "introuvable" in msg
