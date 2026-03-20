# tests/test_lot_fifo_tracker.py
"""Tests for BatchLotTracker — cross-brassin virtual stock consumption."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from common.lot_fifo import BatchLotTracker


def _make_lot(lot_id: int, qty: float, dluo: str = "2026-06-01") -> dict:
    return {
        "idMatierePremiereNumeroLot": lot_id,
        "quantite": qty,
        "dateLimiteUtilisationOptimale": dluo,
        "numeroLot": f"LOT-{lot_id}",
    }


def _make_ingredient(id_mp: int, qty: float, libelle: str = "Sucre") -> dict:
    return {
        "matierePremiere": {"idMatierePremiere": id_mp, "libelle": libelle},
        "quantite": qty,
        "modeleNumerosLots": [],
        "brassageEtape": {"nom": "Aromatisation"},
    }


class TestBatchLotTracker:

    def test_single_ingredient_single_lot(self):
        """Basic case: one ingredient fully covered by one lot."""
        lots = [_make_lot(100, 50.0)]
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: lots)
        result = tracker.distribute_ingredient(_make_ingredient(42, 30.0))
        assert len(result) == 1
        assert result[0]["quantite"] == 30.0
        assert result[0]["modeleNumerosLots"][0]["code"] == "LOT-100"

    def test_virtual_consumption_across_brassins(self):
        """Two brassins consume from same pool — second sees reduced stock."""
        lots = [_make_lot(100, 50.0)]
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: lots)
        # First brassin takes 40
        r1 = tracker.distribute_ingredient(_make_ingredient(42, 40.0))
        assert r1[0]["quantite"] == 40.0
        # Second brassin — only 10 left
        r2 = tracker.distribute_ingredient(_make_ingredient(42, 20.0))
        assert r2[0]["quantite"] == 10.0
        # Shortfall line (no lots)
        assert len(r2) == 2
        assert r2[1]["quantite"] == 10.0
        assert r2[1]["modeleNumerosLots"] == []

    def test_no_lots_returns_ingredient_unchanged(self):
        """If no lots available, return ingredient as-is."""
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: [])
        ing = _make_ingredient(42, 10.0)
        result = tracker.distribute_ingredient(ing)
        assert len(result) == 1
        assert result[0] is ing  # same object, unchanged

    def test_missing_id_mp_returns_unchanged(self):
        """Ingredient without idMatierePremiere → unchanged."""
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: [])
        ing = {"matierePremiere": {}, "quantite": 10.0}
        result = tracker.distribute_ingredient(ing)
        assert result == [ing]

    def test_zero_qty_returns_unchanged(self):
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: [_make_lot(1, 100)])
        ing = _make_ingredient(42, 0)
        result = tracker.distribute_ingredient(ing)
        assert result == [ing]

    def test_fetch_called_once_per_mp(self):
        """API fetch is cached — called once per MP, not per ingredient."""
        mock_fetch = MagicMock(return_value=[_make_lot(100, 100.0)])
        tracker = BatchLotTracker(fetch_lots_fn=mock_fetch)
        tracker.distribute_ingredient(_make_ingredient(42, 10.0))
        tracker.distribute_ingredient(_make_ingredient(42, 10.0))
        mock_fetch.assert_called_once_with(42)

    def test_fetch_failure_returns_unchanged(self):
        """If API fails, ingredient returned unchanged (graceful degradation)."""
        def fail(id_mp):
            raise ConnectionError("API down")
        tracker = BatchLotTracker(fetch_lots_fn=fail)
        ing = _make_ingredient(42, 10.0)
        result = tracker.distribute_ingredient(ing)
        assert len(result) == 1
        assert result[0] is ing

    @patch("common.easybeer._client.is_rate_limited", return_value=5.0)
    def test_rate_limited_skips_fetch(self, mock_rl):
        """If rate-limited, don't call API — return unchanged."""
        mock_fetch = MagicMock(return_value=[_make_lot(100, 100.0)])
        tracker = BatchLotTracker(fetch_lots_fn=mock_fetch)
        ing = _make_ingredient(42, 10.0)
        result = tracker.distribute_ingredient(ing)
        mock_fetch.assert_not_called()
        assert result == [ing]

    def test_split_across_lots(self):
        """Ingredient needs more than one lot → split into multiple lines."""
        lots = [
            _make_lot(100, 20.0, "2026-01-01"),
            _make_lot(200, 30.0, "2026-06-01"),
        ]
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: lots)
        result = tracker.distribute_ingredient(_make_ingredient(42, 35.0))
        assert len(result) == 2
        assert result[0]["modeleNumerosLots"][0]["code"] == "LOT-100"
        assert result[0]["quantite"] == 20.0
        assert result[1]["modeleNumerosLots"][0]["code"] == "LOT-200"
        assert result[1]["quantite"] == 15.0
