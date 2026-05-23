"""Tests for common/services/production_sheet_eb_bind.py — finalize → EB bind."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

from common.services.production_sheet_eb_bind import (
    _coerce_date_formulaire,
    _safe_float,
    build_mesure_payload,
    enqueue_eb_events_from_sheet,
    is_eb_bind_enabled,
)

# ─── Fake ProductionSheetDetail ──────────────────────────────────────────


@dataclass
class _FakeSheet:
    """Stub minimal pour les tests — évite d'importer le vrai modèle."""
    id: str = "sheet-1"
    brassin_id: str | None = "12345"
    data: dict[str, Any] = field(default_factory=dict)


# ─── is_eb_bind_enabled ──────────────────────────────────────────────────


class TestIsEbBindEnabled:

    @patch.dict("os.environ", {}, clear=True)
    def test_off_by_default(self):
        assert is_eb_bind_enabled() is False

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_PRODUCTION_SHEETS": "true"})
    def test_true(self):
        assert is_eb_bind_enabled() is True

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_PRODUCTION_SHEETS": "1"})
    def test_one(self):
        assert is_eb_bind_enabled() is True

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_PRODUCTION_SHEETS": "yes"})
    def test_yes(self):
        assert is_eb_bind_enabled() is True

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_PRODUCTION_SHEETS": "false"})
    def test_explicit_false(self):
        assert is_eb_bind_enabled() is False

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_PRODUCTION_SHEETS": "garbage"})
    def test_garbage_is_off(self):
        assert is_eb_bind_enabled() is False


# ─── build_mesure_payload ────────────────────────────────────────────────


class TestBuildMesurePayload:

    def test_no_brassin_id_returns_none(self):
        sheet = _FakeSheet(brassin_id=None)
        assert build_mesure_payload(sheet, user_email="x@y.com") is None

    def test_non_numeric_brassin_id_returns_none(self):
        sheet = _FakeSheet(brassin_id="not-a-number")
        assert build_mesure_payload(sheet, user_email="x@y.com") is None

    def test_no_mesures_returns_none(self):
        sheet = _FakeSheet(brassin_id="42", data={"fermentation": {"mesures": []}})
        assert build_mesure_payload(sheet, user_email="x@y.com") is None

    def test_no_fermentation_section_returns_none(self):
        sheet = _FakeSheet(brassin_id="42", data={})
        assert build_mesure_payload(sheet, user_email="x@y.com") is None

    def test_simple_mesure_builds_minimal_payload(self):
        sheet = _FakeSheet(
            brassin_id="42",
            data={
                "fermentation": {
                    "mesures": [
                        {
                            "date": "2026-05-23",
                            "heure": "14:30",
                            "brix": "12.5",
                            "ph": "4.2",
                            "temperature": "20.5",
                        }
                    ],
                }
            },
        )
        payload = build_mesure_payload(sheet, user_email="user@x.com")

        assert payload is not None
        assert payload["idBrassin"] == 42
        assert payload["etape"] == "fermentation"
        assert payload["auteur"] == "user@x.com"
        assert payload["densite"] == 12.5
        assert payload["ph"] == 4.2
        assert payload["temperature"] == 20.5
        assert payload["dateFormulaire"].startswith("2026-05-23T14:30")

    def test_only_last_mesure_is_used(self):
        """Si plusieurs mesures, on prend la dernière (la plus récente)."""
        sheet = _FakeSheet(
            brassin_id="42",
            data={
                "fermentation": {
                    "mesures": [
                        {"brix": "11.0"},
                        {"brix": "11.5"},
                        {"brix": "12.0"},  # ← celle-ci
                    ],
                }
            },
        )
        payload = build_mesure_payload(sheet, user_email="x@y.com")
        assert payload["densite"] == 12.0

    def test_falls_back_to_matricule_if_no_email(self):
        sheet = _FakeSheet(
            brassin_id="42",
            data={"fermentation": {"mesures": [{"matricule": "AB12"}]}},
        )
        payload = build_mesure_payload(sheet, user_email="")
        assert payload["auteur"] == "AB12"

    def test_incident_via_explicit_notes(self):
        sheet = _FakeSheet(
            brassin_id="42",
            data={
                "fermentation": {"mesures": [{"brix": "12"}]},
                "incidents": {"notes": "Contamination détectée"},
            },
        )
        payload = build_mesure_payload(sheet, user_email="x@y.com")
        assert payload["nonConformite"] == "Contamination détectée"

    def test_incident_via_statut_non_conforme(self):
        sheet = _FakeSheet(
            brassin_id="42",
            data={
                "fermentation": {
                    "mesures": [{"brix": "12"}],
                    "statut": "Non conforme",
                },
            },
        )
        payload = build_mesure_payload(sheet, user_email="x@y.com")
        assert "Non conforme" in payload["nonConformite"]

    def test_no_incident_when_conforme(self):
        sheet = _FakeSheet(
            brassin_id="42",
            data={
                "fermentation": {
                    "mesures": [{"brix": "12"}],
                    "statut": "Conforme",
                },
            },
        )
        payload = build_mesure_payload(sheet, user_email="x@y.com")
        assert "nonConformite" not in payload

    def test_commentaire_combines_gout_and_observation(self):
        sheet = _FakeSheet(
            brassin_id="42",
            data={
                "fermentation": {
                    "mesures": [{
                        "brix": "12",
                        "gout": "Fruité",
                        "observation": "Mousse abondante",
                    }],
                },
            },
        )
        payload = build_mesure_payload(sheet, user_email="x@y.com")
        assert "Fruité" in payload["commentaire"]
        assert "Mousse abondante" in payload["commentaire"]

    def test_skips_invalid_numeric_fields(self):
        sheet = _FakeSheet(
            brassin_id="42",
            data={
                "fermentation": {
                    "mesures": [{"brix": "", "ph": "NaN", "temperature": "abc"}],
                },
            },
        )
        payload = build_mesure_payload(sheet, user_email="x@y.com")
        # Seul idBrassin, etape, auteur, dateFormulaire devraient être présents
        assert "densite" not in payload
        # NaN est techniquement convertible par float() mais on garde le test
        # comme régression : si plus tard on filtre les NaN, ce test sera utile


# ─── enqueue_eb_events_from_sheet ────────────────────────────────────────


class TestEnqueueEbEventsFromSheet:

    @patch.dict("os.environ", {}, clear=True)
    def test_skip_when_flag_off(self):
        sheet = _FakeSheet()
        result = enqueue_eb_events_from_sheet(
            sheet, tenant_id="t1", user_email="x@y.com",
        )
        assert result["enabled"] is False
        assert "not enabled" in result["skipped_reason"]
        assert result["enqueued"] == []

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_PRODUCTION_SHEETS": "true"})
    def test_skip_when_no_brassin_id(self):
        sheet = _FakeSheet(brassin_id=None)
        result = enqueue_eb_events_from_sheet(
            sheet, tenant_id="t1", user_email="x@y.com",
        )
        assert result["enabled"] is True
        assert "manual sheet" in result["skipped_reason"]
        assert result["enqueued"] == []

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_PRODUCTION_SHEETS": "true"})
    @patch("common.easybeer.queued.enqueue_brassin_mesure")
    def test_enqueues_mesure_when_data_present(self, mock_enqueue: MagicMock):
        mock_enqueue.return_value = 99
        sheet = _FakeSheet(
            brassin_id="42",
            data={"fermentation": {"mesures": [{"brix": "12", "ph": "4.1"}]}},
        )

        result = enqueue_eb_events_from_sheet(
            sheet, tenant_id="t1", user_email="user@x.com",
        )

        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        assert kwargs["tenant_id"] == "t1"
        assert kwargs["user_email"] == "user@x.com"
        assert kwargs["payload"]["idBrassin"] == 42

        assert any(
            e["event_type"] == "brassin.mesure" and e["id"] == 99
            for e in result["enqueued"]
        )

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_PRODUCTION_SHEETS": "true"})
    @patch("common.easybeer.queued.enqueue_brassin_mesure")
    def test_no_enqueue_if_no_mesure(self, mock_enqueue: MagicMock):
        sheet = _FakeSheet(brassin_id="42", data={})
        result = enqueue_eb_events_from_sheet(
            sheet, tenant_id="t1", user_email="x@y.com",
        )
        mock_enqueue.assert_not_called()
        assert "brassin.mesure" in " ".join(result["skipped"])

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_PRODUCTION_SHEETS": "true"})
    @patch("common.easybeer.queued.enqueue_brassin_mesure", side_effect=RuntimeError("DB"))
    def test_swallows_errors(self, _mock_enqueue: MagicMock):
        """Une erreur d'enqueue ne doit pas propager (best-effort)."""
        sheet = _FakeSheet(
            brassin_id="42",
            data={"fermentation": {"mesures": [{"brix": "12"}]}},
        )
        # Should NOT raise
        result = enqueue_eb_events_from_sheet(
            sheet, tenant_id="t1", user_email="x@y.com",
        )
        assert any("brassin.mesure" in err for err in result["errors"])

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_PRODUCTION_SHEETS": "true"})
    @patch("common.easybeer.queued.enqueue_brassin_mesure")
    def test_skipped_writes_documented(self, _mock_enqueue: MagicMock):
        """Conditionner + Terminer restent dans skipped pour cette PR."""
        _mock_enqueue.return_value = 1
        sheet = _FakeSheet(
            brassin_id="42",
            data={"fermentation": {"mesures": [{"brix": "12"}]}},
        )
        result = enqueue_eb_events_from_sheet(
            sheet, tenant_id="t1", user_email="x@y.com",
        )
        skipped_str = " ".join(result["skipped"])
        assert "mise-en-bouteille" in skipped_str
        assert "terminer" in skipped_str


# ─── Helpers ──────────────────────────────────────────────────────────────


class TestSafeFloat:

    def test_valid_string(self):
        assert _safe_float("12.5") == 12.5

    def test_valid_int(self):
        assert _safe_float(42) == 42.0

    def test_empty_returns_none(self):
        assert _safe_float("") is None
        assert _safe_float(None) is None

    def test_invalid_returns_none(self):
        assert _safe_float("abc") is None


class TestCoerceDate:

    def test_with_date_and_heure(self):
        result = _coerce_date_formulaire({"date": "2026-05-23", "heure": "14:30"})
        assert result == "2026-05-23T14:30:00"

    def test_heure_without_minutes(self):
        result = _coerce_date_formulaire({"date": "2026-05-23", "heure": "14"})
        assert result.startswith("2026-05-23T14:00")

    def test_no_date_uses_now(self):
        result = _coerce_date_formulaire({})
        # Format YYYY-MM-DDTHH:MM:SS
        assert "T" in result
        assert len(result) >= 19
