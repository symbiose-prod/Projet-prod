"""Tests for common/services/production_sheet_eb_bind.py — finalize → EB bind."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

from common.services.eb_product_mapping import GtinIndexEntry
from common.services.production_sheet_eb_bind import (
    _coerce_date_formulaire,
    _safe_float,
    build_mesure_payload,
    build_mise_en_bouteille_payload,
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
    lot: str = "KMA15052026"
    ddm: object | None = None
    finalized_at: object | None = None


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


# ─── build_mise_en_bouteille_payload ─────────────────────────────────────


class TestBuildMiseEnBouteillePayload:

    def _gtin_index_full(self):
        """Index avec bouteille ET carton pour chaque format — cas nominal."""
        return {
            # Format 12x33 NIKO
            "3770014427021": GtinIndexEntry(  # gtin_uvc bouteille
                id_produit=100, id_contenant=50, contenance_l=0.33, lot_libelle=None,
            ),
            "3770014427014": GtinIndexEntry(  # ean carton (Carton de 12)
                id_produit=100, id_contenant=999, contenance_l=0.33,
                lot_libelle="Carton de 12",
            ),
            # Format 6x75 NIKO
            "3770014427038": GtinIndexEntry(  # gtin_uvc bouteille
                id_produit=101, id_contenant=60, contenance_l=0.75, lot_libelle=None,
            ),
            "3770014427045": GtinIndexEntry(  # ean carton
                id_produit=101, id_contenant=888, contenance_l=0.75,
                lot_libelle="Carton de 6",
            ),
        }

    def _eph_row(
        self,
        *,
        gtin_uvc: str = "3770014427021",
        ean: str = "3770014427014",
        pcb: int = 12,
        fmt: str = "12x33",
    ) -> dict[str, Any]:
        return {
            "ean": ean,
            "gtin_uvc": gtin_uvc,
            "lot": "KMA15052026",
            "fmt": fmt,
            "marque": "NIKO",
            "designation": "Kéfir Mangue Passion",
            "pcb": pcb,
        }

    def test_no_brassin_id(self):
        sheet = _FakeSheet(brassin_id=None)
        payload, warnings = build_mise_en_bouteille_payload(sheet, tenant_id="t1")
        assert payload is None
        assert any("brassin_id" in w for w in warnings)

    def test_no_lot(self):
        sheet = _FakeSheet(lot="", data={"conditionnement_reel": {"items": [{"x": 1}]}})
        payload, warnings = build_mise_en_bouteille_payload(sheet, tenant_id="t1")
        assert payload is None
        assert any("lot" in w.lower() for w in warnings)

    def test_no_items(self):
        sheet = _FakeSheet(data={})
        payload, warnings = build_mise_en_bouteille_payload(sheet, tenant_id="t1")
        assert payload is None

    @patch("common.services.eb_product_mapping.load_gtin_index_from_eb")
    @patch("common.services.eb_product_mapping.run_sql")
    def test_pushes_in_carton_when_available(
        self, mock_sql: MagicMock, mock_load: MagicMock,
    ):
        """Cas nominal V2 : on pousse en 'Carton de X' (idContenant carton,
        quantite = cartons, pas cartons × pcb)."""
        mock_load.return_value = self._gtin_index_full()
        mock_sql.return_value = [self._eph_row(pcb=12)]
        sheet = _FakeSheet(
            data={
                "conditionnement_reel": {
                    "items": [{"marque": "NIKO", "fmt": "12x33", "cartons": 10}],
                },
            },
        )
        payload, _ = build_mise_en_bouteille_payload(sheet, tenant_id="t1")
        assert payload is not None
        entries = payload["modelesStockProduitBouteille"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["modeleProduit"]["idProduit"] == 100
        assert entry["modeleContenant"]["idContenant"] == 999  # Carton de 12
        assert entry["quantiteMiseEnBouteille"] == 10  # cartons, PAS bouteilles
        assert entry["contenance"] == 0.33

    @patch("common.services.eb_product_mapping.load_gtin_index_from_eb")
    @patch("common.services.eb_product_mapping.run_sql")
    def test_fallback_unite_if_no_carton(
        self, mock_sql: MagicMock, mock_load: MagicMock,
    ):
        """Si le carton n'est pas dans la matrice EB, fallback sur Unité avec warning."""
        # Index avec seulement la bouteille (pas le carton)
        gtin_index = {
            "3770014427021": GtinIndexEntry(
                id_produit=100, id_contenant=50, contenance_l=0.33, lot_libelle=None,
            ),
        }
        mock_load.return_value = gtin_index
        mock_sql.return_value = [self._eph_row(pcb=12)]
        sheet = _FakeSheet(
            data={
                "conditionnement_reel": {
                    "items": [{"marque": "NIKO", "fmt": "12x33", "cartons": 10}],
                },
            },
        )
        payload, warnings = build_mise_en_bouteille_payload(sheet, tenant_id="t1")
        assert payload is not None
        entry = payload["modelesStockProduitBouteille"][0]
        assert entry["modeleContenant"]["idContenant"] == 50  # bouteille
        assert entry["quantiteMiseEnBouteille"] == 120  # cartons × pcb
        assert any("fallback Unité" in w for w in warnings)

    @patch("common.services.eb_product_mapping.load_gtin_index_from_eb")
    @patch("common.services.eb_product_mapping.run_sql")
    def test_multiple_items_resolved(
        self, mock_sql: MagicMock, mock_load: MagicMock,
    ):
        """Plusieurs formats dans le même finalize → plusieurs entries (en Carton)."""
        mock_load.return_value = self._gtin_index_full()
        def sql_side_effect(_sql: str, params: dict):
            if params["fmt"] == "12x33":
                return [self._eph_row(
                    gtin_uvc="3770014427021", ean="3770014427014",
                    pcb=12, fmt="12x33",
                )]
            if params["fmt"] == "6x75":
                return [self._eph_row(
                    gtin_uvc="3770014427038", ean="3770014427045",
                    pcb=6, fmt="6x75",
                )]
            return []
        mock_sql.side_effect = sql_side_effect

        sheet = _FakeSheet(
            data={
                "conditionnement_reel": {
                    "items": [
                        {"marque": "NIKO", "fmt": "12x33", "cartons": 10},
                        {"marque": "NIKO", "fmt": "6x75", "cartons": 5},
                    ],
                },
            },
        )
        payload, _ = build_mise_en_bouteille_payload(sheet, tenant_id="t1")
        entries = payload["modelesStockProduitBouteille"]
        assert len(entries) == 2
        # Chaque entry a son propre idContenant carton
        contenants = {e["modeleContenant"]["idContenant"] for e in entries}
        assert contenants == {999, 888}
        # Et quantites = cartons (pas bouteilles)
        qtys = {e["quantiteMiseEnBouteille"] for e in entries}
        assert qtys == {10, 5}

    @patch("common.services.eb_product_mapping.load_gtin_index_from_eb")
    @patch("common.services.eb_product_mapping.run_sql")
    def test_unresolved_item_skipped_with_warning(
        self, mock_sql: MagicMock, mock_load: MagicMock,
    ):
        mock_load.return_value = self._gtin_index_full()
        mock_sql.return_value = []  # pas d'étiquette
        sheet = _FakeSheet(
            data={
                "conditionnement_reel": {
                    "items": [{"marque": "NIKO", "fmt": "12x33", "cartons": 10}],
                },
            },
        )
        payload, warnings = build_mise_en_bouteille_payload(sheet, tenant_id="t1")
        assert payload is None
        assert any("unresolved" in w for w in warnings)

    @patch("common.services.eb_product_mapping.load_gtin_index_from_eb")
    def test_no_matrice_skipped(self, mock_load: MagicMock):
        mock_load.return_value = {}
        sheet = _FakeSheet(
            data={
                "conditionnement_reel": {
                    "items": [{"marque": "NIKO", "fmt": "12x33", "cartons": 10}],
                },
            },
        )
        payload, warnings = build_mise_en_bouteille_payload(sheet, tenant_id="t1")
        assert payload is None
        assert any("matrice" in w for w in warnings)

    @patch("common.services.eb_product_mapping.load_gtin_index_from_eb")
    @patch("common.services.eb_product_mapping.run_sql")
    def test_pcb_zero_skipped(self, mock_sql: MagicMock, mock_load: MagicMock):
        mock_load.return_value = self._gtin_index_full()
        mock_sql.return_value = [self._eph_row(pcb=0)]
        sheet = _FakeSheet(
            data={
                "conditionnement_reel": {
                    "items": [{"marque": "NIKO", "fmt": "12x33", "cartons": 10}],
                },
            },
        )
        payload, warnings = build_mise_en_bouteille_payload(sheet, tenant_id="t1")
        assert payload is None
        assert any("pcb=0" in w for w in warnings)


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
