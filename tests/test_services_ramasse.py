"""Tests for common/services/ramasse_service — pure service layer.

Mocke les appels EasyBeer pour vérifier la logique d'orchestration :
- dedup archives / en-cours,
- filtre annulés,
- collection d'erreurs quand un endpoint échoue,
- fallback None gracieux pour cb_matrix / weights / entrepot,
- agrégation finale par load_initial_data.

Couvre aussi les helpers purs d'envoi : compute_totals, build_lines_payload,
build_email_subject, build_email_body.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
import requests

from common.easybeer import EasyBeerError
from common.services.ramasse_service import (
    RamasseInitialData,
    RamasseTotals,
    build_email_body,
    build_email_subject,
    build_lines_payload,
    compute_totals,
    load_active_brassins,
    load_barcode_matrix,
    load_carton_weights,
    load_initial_data,
    load_main_entrepot_id,
)

# ─── load_active_brassins ──────────────────────────────────────────────────

class TestLoadActiveBrassins:
    @patch("common.services.ramasse_service.get_brassins_archives")
    @patch("common.services.ramasse_service.get_brassins_en_cours")
    def test_happy_path_merges_active_and_archives(self, mock_ec, mock_ar):
        mock_ec.return_value = [
            {"idBrassin": 1, "nom": "Brassin A"},
            {"idBrassin": 2, "nom": "Brassin B"},
        ]
        mock_ar.return_value = [
            {"idBrassin": 3, "nom": "Brassin C archivé"},
        ]
        brassins, errors = load_active_brassins(nb_archives=3)
        assert errors == []
        assert len(brassins) == 3
        # C est archivé
        c = next(b for b in brassins if b["idBrassin"] == 3)
        assert c["_is_archive"] is True
        a = next(b for b in brassins if b["idBrassin"] == 1)
        assert "_is_archive" not in a

    @patch("common.services.ramasse_service.get_brassins_archives")
    @patch("common.services.ramasse_service.get_brassins_en_cours")
    def test_dedup_archive_already_in_active(self, mock_ec, mock_ar):
        """Un brassin présent en en-cours ET en archives ne doit pas être dupliqué."""
        mock_ec.return_value = [{"idBrassin": 1, "nom": "A"}]
        mock_ar.return_value = [{"idBrassin": 1, "nom": "A"}]  # même id
        brassins, errors = load_active_brassins()
        assert errors == []
        assert len(brassins) == 1
        assert "_is_archive" not in brassins[0]

    @patch("common.services.ramasse_service.get_brassins_archives")
    @patch("common.services.ramasse_service.get_brassins_en_cours")
    def test_cancelled_brassins_filtered(self, mock_ec, mock_ar):
        mock_ec.return_value = [
            {"idBrassin": 1, "nom": "A"},
            {"idBrassin": 2, "nom": "B annulé", "annule": True},
        ]
        mock_ar.return_value = []
        brassins, _ = load_active_brassins()
        assert [b["idBrassin"] for b in brassins] == [1]

    @patch("common.services.ramasse_service.get_brassins_archives")
    @patch("common.services.ramasse_service.get_brassins_en_cours")
    def test_en_cours_error_collected_not_raised(self, mock_ec, mock_ar):
        mock_ec.side_effect = EasyBeerError("500 EB down")
        mock_ar.return_value = [{"idBrassin": 10, "nom": "Archive X"}]
        brassins, errors = load_active_brassins()
        assert len(errors) == 1
        assert "Brassins en cours" in errors[0]
        # on a quand même les archives, marquées _is_archive
        assert len(brassins) == 1
        assert brassins[0]["_is_archive"] is True

    @patch("common.services.ramasse_service.get_brassins_archives")
    @patch("common.services.ramasse_service.get_brassins_en_cours")
    def test_archives_error_collected(self, mock_ec, mock_ar):
        mock_ec.return_value = [{"idBrassin": 1, "nom": "A"}]
        mock_ar.side_effect = requests.RequestException("timeout")
        brassins, errors = load_active_brassins()
        assert len(errors) == 1
        assert "Brassins archivés" in errors[0]
        assert [b["idBrassin"] for b in brassins] == [1]


# ─── load_barcode_matrix ───────────────────────────────────────────────────

class TestLoadBarcodeMatrix:
    @patch("common.services.ramasse_service.parse_barcode_matrix")
    @patch("common.services.ramasse_service.get_code_barre_matrice")
    def test_happy_path(self, mock_get, mock_parse):
        mock_get.return_value = [{"raw": "stuff"}]
        mock_parse.return_value = {42: [{"code": "3760"}]}
        result = load_barcode_matrix()
        assert result == {42: [{"code": "3760"}]}

    @patch("common.services.ramasse_service.get_code_barre_matrice",
           side_effect=EasyBeerError("timeout"))
    def test_fails_gracefully_returns_none(self, _mock):
        assert load_barcode_matrix() is None

    @patch("common.services.ramasse_service.get_code_barre_matrice",
           side_effect=requests.ConnectionError("net"))
    def test_request_exception_returns_none(self, _mock):
        assert load_barcode_matrix() is None


# ─── load_carton_weights ───────────────────────────────────────────────────

class TestLoadCartonWeights:
    @patch("common.services.ramasse_service.fetch_carton_weights")
    def test_happy_path(self, mock_fetch):
        mock_fetch.return_value = {(1, "6x75"): 6.8}
        assert load_carton_weights() == {(1, "6x75"): 6.8}

    @patch("common.services.ramasse_service.fetch_carton_weights",
           side_effect=EasyBeerError("banned"))
    def test_fails_gracefully(self, _mock):
        assert load_carton_weights() is None


# ─── load_main_entrepot_id ─────────────────────────────────────────────────

class TestLoadMainEntrepotId:
    @patch("common.services.ramasse_service.get_warehouses")
    def test_picks_principal(self, mock_gw):
        mock_gw.return_value = [
            {"idEntrepot": 1, "principal": False},
            {"idEntrepot": 2, "principal": True},
            {"idEntrepot": 3, "principal": False},
        ]
        assert load_main_entrepot_id() == 2

    @patch("common.services.ramasse_service.get_warehouses")
    def test_fallback_first_if_none_principal(self, mock_gw):
        mock_gw.return_value = [
            {"idEntrepot": 10},
            {"idEntrepot": 11},
        ]
        assert load_main_entrepot_id() == 10

    @patch("common.services.ramasse_service.get_warehouses", return_value=[])
    def test_empty_list_returns_none(self, _mock):
        assert load_main_entrepot_id() is None

    @patch("common.services.ramasse_service.get_warehouses",
           side_effect=EasyBeerError("500"))
    def test_api_error_returns_none(self, _mock):
        assert load_main_entrepot_id() is None


# ─── load_initial_data (agrégation) ────────────────────────────────────────

class TestLoadInitialData:
    @patch("common.services.ramasse_service.load_carton_weights")
    @patch("common.services.ramasse_service.load_main_entrepot_id")
    @patch("common.services.ramasse_service.load_barcode_matrix")
    @patch("common.services.ramasse_service.load_active_brassins")
    def test_packs_all_fetches_into_dataclass(
        self, mock_brassins, mock_cb, mock_ent, mock_weights,
    ):
        mock_brassins.return_value = ([{"idBrassin": 1}], ["err1"])
        mock_cb.return_value = {42: []}
        mock_ent.return_value = 99
        mock_weights.return_value = {(1, "6x75"): 5.0}

        data = load_initial_data()
        assert isinstance(data, RamasseInitialData)
        assert data.brassins == [{"idBrassin": 1}]
        assert data.brassin_load_errors == ["err1"]
        assert data.cb_by_product == {42: []}
        assert data.id_entrepot == 99
        assert data.eb_weights == {(1, "6x75"): 5.0}

    @patch("common.services.ramasse_service.load_carton_weights", return_value=None)
    @patch("common.services.ramasse_service.load_main_entrepot_id", return_value=None)
    @patch("common.services.ramasse_service.load_barcode_matrix", return_value=None)
    @patch("common.services.ramasse_service.load_active_brassins",
           return_value=([], ["tout a échoué"]))
    def test_full_failure_mode(self, *_mocks):
        """Tous les fetchs échouent → dataclass remplie de None / [] / erreurs."""
        data = load_initial_data()
        assert data.brassins == []
        assert data.brassin_load_errors == ["tout a échoué"]
        assert data.cb_by_product is None
        assert data.id_entrepot is None
        assert data.eb_weights is None

    @patch("common.services.ramasse_service.load_carton_weights", return_value=None)
    @patch("common.services.ramasse_service.load_main_entrepot_id", return_value=42)
    @patch("common.services.ramasse_service.load_barcode_matrix", return_value={1: []})
    @patch("common.services.ramasse_service.load_active_brassins",
           return_value=([{"idBrassin": 5}], []))
    def test_partial_failure_non_blocking(self, *_mocks):
        """Si un fetch renvoie None, les autres passent : la page rendra en mode dégradé."""
        data = load_initial_data()
        assert data.brassins == [{"idBrassin": 5}]
        assert data.id_entrepot == 42
        assert data.eb_weights is None  # le fetch poids a échoué — pas de crash


@pytest.fixture
def _no_parallel(monkeypatch):
    """Force l'exécution séquentielle du ThreadPoolExecutor pour tests reproductibles.

    Pas strictement nécessaire (les mocks sont synchrones) mais protège des
    artefacts potentiels de scheduling.
    """
    from concurrent import futures as _fut

    class _SeqExec:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *_): return False

        def submit(self, fn, *args, **kw):
            f = _fut.Future()
            try:
                f.set_result(fn(*args, **kw))
            except Exception as e:  # noqa: BLE001
                f.set_exception(e)
            return f

    monkeypatch.setattr(
        "common.services.ramasse_service.ThreadPoolExecutor", _SeqExec,
    )


# ─── compute_totals ────────────────────────────────────────────────────────

class TestComputeTotals:
    def test_basic_sum(self):
        rows = [
            {"cartons": 10, "palettes": 2, "poids": 120},
            {"cartons": 5, "palettes": 1, "poids": 60},
        ]
        t = compute_totals(rows)
        assert isinstance(t, RamasseTotals)
        assert t.cartons == 15
        assert t.palettes == 3
        assert t.poids_kg == 180

    def test_empty_rows(self):
        t = compute_totals([])
        assert t.cartons == 0 and t.palettes == 0 and t.poids_kg == 0

    def test_none_values_coerced_to_zero(self):
        """Les cellules Quasar peuvent renvoyer None après effacement — pas de crash."""
        rows = [
            {"cartons": None, "palettes": 2, "poids": 50},
            {"cartons": 3, "palettes": None, "poids": None},
        ]
        t = compute_totals(rows)
        assert t.cartons == 3
        assert t.palettes == 2
        assert t.poids_kg == 50

    def test_missing_keys_default_zero(self):
        rows = [{"cartons": 4}]  # palettes, poids absents
        t = compute_totals(rows)
        assert t.cartons == 4 and t.palettes == 0 and t.poids_kg == 0


# ─── build_lines_payload ───────────────────────────────────────────────────

class TestBuildLinesPayload:
    def test_keeps_only_persisted_columns(self):
        rows = [{
            "ref": "K-ORI-6x75",
            "produit": "Original · 6x75cl",
            "ddm": "01/01/2027",
            "cartons": 10,
            "palettes": 2,
            "poids": 120,
            # Colonnes éphémères (poids_display, _sep, _gout, etc.)
            "poids_display": "120 kg",
            "_sep": False,
            "_gout": "Original",
        }]
        out = build_lines_payload(rows)
        assert out == [{
            "ref": "K-ORI-6x75",
            "produit": "Original · 6x75cl",
            "ddm": "01/01/2027",
            "cartons": 10,
            "palettes": 2,
            "poids": 120,
        }]

    def test_defensive_on_missing_and_none(self):
        rows = [{}]
        out = build_lines_payload(rows)
        assert out == [{
            "ref": "", "produit": "", "ddm": "",
            "cartons": 0, "palettes": 0, "poids": 0,
        }]


# ─── build_email_subject ───────────────────────────────────────────────────

class TestBuildEmailSubject:
    def test_creation_v1(self):
        s = build_email_subject(date(2026, 4, 19))
        assert s == "Demande de ramasse — 19/04/2026 — Ferment Station"

    def test_update_v2_has_version(self):
        s = build_email_subject(date(2026, 4, 19), is_update=True, version=2)
        assert "Mise à jour" in s
        assert "v2" in s
        assert "19/04/2026" in s


# ─── build_email_body ──────────────────────────────────────────────────────

class TestBuildEmailBody:
    def test_v1_mentions_total_palettes(self):
        body = build_email_body(
            date(2026, 4, 19),
            total_palettes=3,
            total_cartons=30,
        )
        assert "3</strong> palettes" in body  # pluriel
        assert "19/04/2026" in body
        assert "Ferment Station" in body  # signature
        # Pas de mention de version (mode création)
        assert "version" not in body.lower()
        # Pas de mention d'update/remplace
        assert "remplace" not in body

    def test_v1_singular_palette(self):
        body = build_email_body(
            date(2026, 4, 19),
            total_palettes=1,
            total_cartons=10,
        )
        # Pas de "s" : "1 palette." pas "1 palettes."
        assert "1</strong> palette." in body or "1</strong> palette<" in body
        assert "palettes" not in body.split("Pour <strong>")[1][:30]

    def test_v2_update_mentions_remplace(self):
        body = build_email_body(
            date(2026, 4, 19),
            total_palettes=5,
            total_cartons=50,
            is_update=True,
            version=3,
        )
        assert "mise à jour" in body.lower()
        assert "version 3" in body
        assert "remplace" in body
        # Mention du PDF différentiel
        assert "jaune" in body
        assert "bleu" in body

    def test_packaging_block_added(self):
        body = build_email_body(
            date(2026, 4, 19),
            total_palettes=1, total_cartons=10,
            packaging_lines=[
                {"qty": 2, "unit": "palette", "label": "Palette bois"},
                {"qty": 5, "unit": "carton", "label": "Cartons vides"},
            ],
        )
        assert "Emballages à ramener" in body
        assert "2 palette(s) Palette bois" in body
        assert "5 carton(s) Cartons vides" in body

    def test_no_packaging_no_block(self):
        body = build_email_body(
            date(2026, 4, 19),
            total_palettes=1, total_cartons=10,
        )
        assert "Emballages à ramener" not in body

    def test_empty_packaging_list_treated_as_none(self):
        body = build_email_body(
            date(2026, 4, 19),
            total_palettes=1, total_cartons=10,
            packaging_lines=[],
        )
        assert "Emballages à ramener" not in body
