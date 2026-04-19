"""Tests for common.ramasse_draft — auto-save des fiches de ramasse."""
from __future__ import annotations

import time
from unittest.mock import patch

from common.ramasse_draft import (
    _KEY,
    clear_draft,
    draft_age_human,
    load_draft,
    save_draft,
)


class _FakeStore(dict):
    """Mimics nicegui app.storage.user (a plain dict)."""


def _with_fake_store(store: _FakeStore):
    """Patch ``_storage()`` to return our fake dict."""
    return patch("common.ramasse_draft._storage", return_value=store)


class TestSaveLoad:
    def test_save_and_load_roundtrip(self):
        store = _FakeStore()
        with _with_fake_store(store):
            save_draft(
                date_iso="2026-04-19",
                destinataire="SOFRIPA Lyon",
                brassin_ids=[123, 456],
                cartons={"REF1": 5, "REF2": 3},
                palettes={"REF1": 2},
            )
            draft = load_draft()
        assert draft is not None
        assert draft["date_iso"] == "2026-04-19"
        assert draft["destinataire"] == "SOFRIPA Lyon"
        assert draft["cartons"] == {"REF1": 5, "REF2": 3}
        assert draft["palettes"] == {"REF1": 2}
        assert draft["brassin_ids"] == [123, 456]
        assert draft["saved_at"] > 0

    def test_empty_draft_not_saved(self):
        """Pas de saisie = pas de brouillon (évite pollution de storage)."""
        store = _FakeStore()
        with _with_fake_store(store):
            save_draft(
                date_iso="2026-04-19",
                destinataire="SOFRIPA",
                brassin_ids=[],
                cartons={},
                palettes={},
            )
        assert _KEY not in store

    def test_cartons_zero_filtered_out(self):
        store = _FakeStore()
        with _with_fake_store(store):
            save_draft(
                date_iso="2026-04-19",
                destinataire="SOFRIPA",
                brassin_ids=[],
                cartons={"REF1": 5, "REF2": 0, "REF3": 0},
            )
            draft = load_draft()
        assert draft is not None
        assert draft["cartons"] == {"REF1": 5}

    def test_no_storage_graceful(self):
        """Sans session NiceGUI, les fonctions ne lèvent rien."""
        with patch("common.ramasse_draft._storage", return_value=None):
            save_draft(
                date_iso="2026-04-19",
                destinataire="X",
                brassin_ids=[],
                cartons={"A": 1},
            )
            assert load_draft() is None
            clear_draft()  # ne doit pas lever


class TestExpiry:
    def test_expired_draft_is_cleared(self):
        store = _FakeStore()
        # Simule un draft vieux de 25h
        store[_KEY] = {
            "date_iso": "2026-04-18",
            "destinataire": "SOFRIPA",
            "brassin_ids": [],
            "cartons": {"A": 1},
            "palettes": {},
            "packaging": {},
            "saved_at": int(time.time()) - 25 * 3600,
        }
        with _with_fake_store(store):
            assert load_draft() is None
            # Nettoyage effectué
            assert _KEY not in store

    def test_recent_draft_loaded(self):
        store = _FakeStore()
        store[_KEY] = {
            "date_iso": "2026-04-19",
            "destinataire": "SOFRIPA",
            "brassin_ids": [],
            "cartons": {"A": 1},
            "palettes": {},
            "packaging": {},
            "saved_at": int(time.time()) - 300,  # 5 min
        }
        with _with_fake_store(store):
            draft = load_draft()
        assert draft is not None
        assert draft["cartons"] == {"A": 1}


class TestClear:
    def test_clear_removes_draft(self):
        store = _FakeStore()
        with _with_fake_store(store):
            save_draft(
                date_iso="2026-04-19",
                destinataire="X",
                brassin_ids=[],
                cartons={"A": 1},
            )
            assert load_draft() is not None
            clear_draft()
            assert load_draft() is None


class TestDraftAgeHuman:
    def test_seconds(self):
        assert draft_age_human({"saved_at": int(time.time()) - 10}) == "il y a quelques secondes"

    def test_minutes(self):
        assert "min" in draft_age_human({"saved_at": int(time.time()) - 300})

    def test_hours(self):
        assert "h" in draft_age_human({"saved_at": int(time.time()) - 7200})

    def test_missing_ts(self):
        assert draft_age_human({}) == "à l'instant"
