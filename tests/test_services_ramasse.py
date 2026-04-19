"""Tests for common/services/ramasse_service — pure service layer.

Mocke les appels EasyBeer pour vérifier la logique d'orchestration :
- dedup archives / en-cours,
- filtre annulés,
- collection d'erreurs quand un endpoint échoue,
- fallback None gracieux pour cb_matrix / weights / entrepot,
- agrégation finale par load_initial_data.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from common.easybeer import EasyBeerError
from common.services.ramasse_service import (
    RamasseInitialData,
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
