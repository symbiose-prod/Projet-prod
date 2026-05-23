"""Tests for common/easybeer/production_writes.py — EB production writes."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from common.easybeer.production_writes import (
    _invalidate_caches_after_production_write,
    conditionner_brassin,
    enregistrer_mesure_brassin,
    enregistrer_sortie_stock,
    terminer_brassin,
)

# ─── conditionner_brassin ────────────────────────────────────────────────


class TestConditionnerBrassin:

    @patch("common.easybeer.production_writes._invalidate_caches_after_production_write")
    @patch("common.easybeer.production_writes.execute_endpoint")
    def test_calls_execute_endpoint_with_correct_path(
        self,
        mock_execute: MagicMock,
        _mock_invalidate: MagicMock,
    ):
        mock_execute.return_value = {"id": 42}
        payload = {"numeroLot": "LOT123", "volumeRestant": 100.0}

        result = conditionner_brassin(payload)

        assert result == {"id": 42}
        mock_execute.assert_called_once_with(
            method="POST",
            path="brassin/mise-en-bouteille",
            payload=payload,
        )

    @patch("common.easybeer.production_writes._invalidate_caches_after_production_write")
    @patch("common.easybeer.production_writes.execute_endpoint")
    def test_invalidates_correct_caches(
        self,
        mock_execute: MagicMock,
        mock_invalidate: MagicMock,
    ):
        mock_execute.return_value = {}
        conditionner_brassin({"x": 1})

        mock_invalidate.assert_called_once()
        keys = mock_invalidate.call_args[0][0]
        assert "brassins_en_cours" in keys
        assert "brassins_planifies" in keys
        assert "stocks_produits" in keys
        assert "autonomie_stocks" in keys


# ─── enregistrer_mesure_brassin ──────────────────────────────────────────


class TestEnregistrerMesure:

    @patch("common.easybeer.production_writes.execute_endpoint")
    def test_calls_execute_endpoint_with_correct_path(self, mock_execute: MagicMock):
        mock_execute.return_value = {"id": 1}
        payload = {"idBrassin": 42, "densite": 1.012, "temperature": 18.5}

        result = enregistrer_mesure_brassin(payload)

        assert result == {"id": 1}
        mock_execute.assert_called_once_with(
            method="POST",
            path="brassin/mesure/enregistrer",
            payload=payload,
        )

    @patch("common.easybeer.production_writes.execute_endpoint")
    def test_supports_incident_via_non_conformite(self, mock_execute: MagicMock):
        """Un incident = mesure avec nonConformite rempli."""
        mock_execute.return_value = {}
        payload = {
            "idBrassin": 42,
            "etape": "fermentation",
            "nonConformite": "Contamination détectée — bactéries lactiques",
        }
        enregistrer_mesure_brassin(payload)

        # Le payload est passé tel quel à EB
        called_payload = mock_execute.call_args.kwargs["payload"]
        assert called_payload["nonConformite"].startswith("Contamination")


# ─── terminer_brassin ────────────────────────────────────────────────────


class TestTerminerBrassin:

    @patch("common.easybeer.production_writes._invalidate_caches_after_production_write")
    @patch("common.easybeer.production_writes.execute_endpoint")
    def test_calls_execute_endpoint_with_correct_path(
        self,
        mock_execute: MagicMock,
        _mock_invalidate: MagicMock,
    ):
        mock_execute.return_value = {"id": 42, "archive": True}
        payload = {"id": 42, "archive": True}

        result = terminer_brassin(payload)

        assert result == {"id": 42, "archive": True}
        mock_execute.assert_called_once_with(
            method="POST",
            path="brassin/terminer",
            payload=payload,
        )

    @patch("common.easybeer.production_writes._invalidate_caches_after_production_write")
    @patch("common.easybeer.production_writes.execute_endpoint")
    def test_invalidates_brassin_listings(
        self,
        mock_execute: MagicMock,
        mock_invalidate: MagicMock,
    ):
        mock_execute.return_value = {}
        terminer_brassin({"id": 1})

        keys = mock_invalidate.call_args[0][0]
        assert "brassins_en_cours" in keys
        assert "brassins_planifies" in keys


# ─── enregistrer_sortie_stock ────────────────────────────────────────────


class TestEnregistrerSortieStock:

    @patch("common.easybeer.production_writes._invalidate_caches_after_production_write")
    @patch("common.easybeer.production_writes.execute_endpoint")
    def test_calls_execute_endpoint_with_correct_path(
        self,
        mock_execute: MagicMock,
        _mock_invalidate: MagicMock,
    ):
        mock_execute.return_value = {"id": 99}
        payload = {
            "idClient": 1234,
            "idProduit": 5678,
            "identifiantLot": "LOT-2026-001",
            "quantite": 240,
        }

        result = enregistrer_sortie_stock(payload)

        assert result == {"id": 99}
        mock_execute.assert_called_once_with(
            method="POST",
            path="stock/sortie/enregistrer",
            payload=payload,
        )

    @patch("common.easybeer.production_writes._invalidate_caches_after_production_write")
    @patch("common.easybeer.production_writes.execute_endpoint")
    def test_invalidates_stock_caches(
        self,
        mock_execute: MagicMock,
        mock_invalidate: MagicMock,
    ):
        mock_execute.return_value = {}
        enregistrer_sortie_stock({"x": 1})

        keys = mock_invalidate.call_args[0][0]
        assert "stocks_produits" in keys
        assert "autonomie_stocks" in keys


# ─── _invalidate_caches_after_production_write ───────────────────────────


class TestInvalidateCaches:

    @patch("common.eb_cache.cache_delete")
    @patch("common._session.current_tenant_id")
    def test_invalidates_each_key(
        self,
        mock_tenant_id: MagicMock,
        mock_cache_delete: MagicMock,
    ):
        mock_tenant_id.return_value = "tenant-1"
        _invalidate_caches_after_production_write(("foo", "bar", "baz"))

        assert mock_cache_delete.call_count == 3
        calls = mock_cache_delete.call_args_list
        keys_called = [c.args[1] for c in calls]
        assert keys_called == ["foo", "bar", "baz"]

    @patch("common.eb_cache.cache_delete")
    @patch("common._session.current_tenant_id")
    def test_no_op_if_no_tenant(
        self,
        mock_tenant_id: MagicMock,
        mock_cache_delete: MagicMock,
    ):
        """Si pas de tenant en contexte (worker hors session web), on skip."""
        mock_tenant_id.return_value = None
        _invalidate_caches_after_production_write(("foo",))

        mock_cache_delete.assert_not_called()

    @patch("common.eb_cache.cache_delete", side_effect=RuntimeError("DB down"))
    @patch("common._session.current_tenant_id")
    def test_swallows_per_key_errors(
        self,
        mock_tenant_id: MagicMock,
        _mock_cache_delete: MagicMock,
    ):
        """Une erreur sur une clé ne bloque pas les suivantes ni n'échoue le worker."""
        mock_tenant_id.return_value = "tenant-1"
        # Should NOT raise
        _invalidate_caches_after_production_write(("foo", "bar"))


# ─── Sanity check : les 4 fonctions ont le décorateur @retry_api ─────────


class TestRetryApiDecorator:

    def test_functions_have_retry_api(self):
        """@retry_api enrobe les fonctions — tenacity les expose via __wrapped__."""
        for fn in (
            conditionner_brassin,
            enregistrer_mesure_brassin,
            terminer_brassin,
            enregistrer_sortie_stock,
        ):
            assert hasattr(fn, "retry") or hasattr(fn, "__wrapped__"), (
                f"{fn.__name__} should be wrapped with @retry_api"
            )
