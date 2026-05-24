"""Tests for common/outbox/handlers.py — event_type dispatcher."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from common.outbox.handlers import EVENT_HANDLERS, UnknownEventType, dispatch


class TestEventHandlersRegistry:

    def test_sprint1_events_registered(self):
        """Les events du Sprint 1 sont toujours présents."""
        assert "brassin.create" in EVENT_HANDLERS
        assert "brassin.planification.add" in EVENT_HANDLERS
        assert "brassin.planification.delete" in EVENT_HANDLERS

    def test_sprint2_events_registered(self):
        """Les nouveaux events Sprint 2 sont câblés."""
        assert "brassin.mise-en-bouteille" in EVENT_HANDLERS
        assert "brassin.mesure" in EVENT_HANDLERS
        assert "brassin.terminer" in EVENT_HANDLERS
        assert "stock.sortie" in EVENT_HANDLERS

    def test_all_handlers_are_callable(self):
        """Sanity check : chaque handler est callable."""
        for event_type, handler in EVENT_HANDLERS.items():
            assert callable(handler), f"{event_type} handler is not callable"


class TestDispatch:

    def test_unknown_event_type_raises(self):
        with pytest.raises(UnknownEventType):
            dispatch("brassin.weird-action", {})

    @patch("common.services.mise_en_bouteille_orchestrator.execute_mise_en_bouteille")
    def test_dispatch_brassin_mise_en_bouteille(self, mock_fn: MagicMock):
        """Le handler délègue à l'orchestrator (couche service)."""
        mock_fn.return_value = {"message": "", "map": {}}
        payload = {"idBrassin": 1, "tenantId": "t", "numeroLot": "L1",
                   "dateMiseEnBouteille": "now", "items": [{}]}
        result = dispatch("brassin.mise-en-bouteille", payload)
        assert result == {"message": "", "map": {}}
        mock_fn.assert_called_once_with(payload)

    @patch("common.easybeer.production_writes.enregistrer_mesure_brassin")
    def test_dispatch_brassin_mesure(self, mock_fn: MagicMock):
        mock_fn.return_value = {"ok": True}
        payload = {"idBrassin": 1, "nonConformite": "test incident"}
        dispatch("brassin.mesure", payload)
        mock_fn.assert_called_once_with(payload)

    @patch("common.easybeer.production_writes.terminer_brassin")
    def test_dispatch_brassin_terminer(self, mock_fn: MagicMock):
        mock_fn.return_value = {"id": 5, "archive": True}
        dispatch("brassin.terminer", {"id": 5, "archive": True})
        mock_fn.assert_called_once()

    @patch("common.easybeer.production_writes.enregistrer_sortie_stock")
    def test_dispatch_stock_sortie(self, mock_fn: MagicMock):
        mock_fn.return_value = {"id": 99}
        dispatch("stock.sortie", {"idClient": 1234, "quantite": 240})
        mock_fn.assert_called_once()

    @patch("common.easybeer.brassins.create_brassin")
    def test_dispatch_brassin_create_sprint1(self, mock_fn: MagicMock):
        """Régression — le handler Sprint 1 fonctionne toujours."""
        mock_fn.return_value = {"id": 1}
        dispatch("brassin.create", {"nom": "Test"})
        mock_fn.assert_called_once_with({"nom": "Test"})
