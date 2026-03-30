"""Tests for common/lot_fifo.py — BatchLotTracker (distribute_ingredient)."""
from __future__ import annotations

from unittest.mock import MagicMock

from common.lot_fifo import BatchLotTracker


def _make_lot(lot_id: int, qty: float, dluo: str = "2026-06-01") -> dict:
    return {
        "idMatierePremiereNumeroLot": lot_id,
        "quantite": qty,
        "dateLimiteUtilisationOptimale": dluo,
        "numeroLot": f"LOT-{lot_id}",
    }


def _make_ingredient(
    id_mp: int, qty: float, libelle: str = "Test MP", etape: str = "Aromatisation"
) -> dict:
    return {
        "matierePremiere": {"idMatierePremiere": id_mp, "libelle": libelle},
        "quantite": qty,
        "brassageEtape": {"nom": etape},
        "modeleNumerosLots": [],
    }


class TestBatchLotTracker:

    def test_distribute_no_mp_id(self):
        """Ingredient without idMatierePremiere → returned as-is."""
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: [])
        ing = {"matierePremiere": {}, "quantite": 10}
        result = tracker.distribute_ingredient(ing)
        assert result == [ing]

    def test_distribute_zero_qty(self):
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: [])
        ing = _make_ingredient(42, 0)
        result = tracker.distribute_ingredient(ing)
        assert result == [ing]

    def test_distribute_negative_qty(self):
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: [])
        ing = _make_ingredient(42, -5)
        result = tracker.distribute_ingredient(ing)
        assert result == [ing]

    def test_distribute_no_lots_available(self):
        """MP has no lots → returned as-is."""
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: [])
        ing = _make_ingredient(42, 10)
        result = tracker.distribute_ingredient(ing)
        assert result == [ing]

    def test_distribute_single_lot_exact(self):
        """Single lot covers exactly the needed quantity."""
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: [_make_lot(100, 10.0)])
        ing = _make_ingredient(42, 10.0)
        result = tracker.distribute_ingredient(ing)
        assert len(result) == 1
        assert result[0]["quantite"] == 10.0
        assert result[0]["modeleNumerosLots"][0]["code"] == "LOT-100"

    def test_distribute_split_across_lots(self):
        """Need exceeds first lot → split across two lots."""
        lots = [_make_lot(100, 5.0, "2026-01-01"), _make_lot(200, 10.0, "2026-06-01")]
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: lots)
        ing = _make_ingredient(42, 8.0)
        result = tracker.distribute_ingredient(ing)
        assert len(result) == 2
        assert result[0]["quantite"] == 5.0
        assert result[1]["quantite"] == 3.0

    def test_distribute_shortfall_extra_line(self):
        """Not enough stock → extra line without lot for the shortfall."""
        tracker = BatchLotTracker(fetch_lots_fn=lambda _: [_make_lot(100, 3.0)])
        ing = _make_ingredient(42, 10.0)
        result = tracker.distribute_ingredient(ing)
        assert len(result) == 2
        assert result[0]["quantite"] == 3.0
        assert result[0]["modeleNumerosLots"][0]["code"] == "LOT-100"
        assert result[1]["quantite"] == 7.0
        assert result[1]["modeleNumerosLots"] == []

    def test_pool_cached_across_ingredients(self):
        """Same MP fetched once, shared across ingredients."""
        fetch = MagicMock(return_value=[_make_lot(100, 20.0)])
        tracker = BatchLotTracker(fetch_lots_fn=fetch)

        tracker.distribute_ingredient(_make_ingredient(42, 5.0))
        tracker.distribute_ingredient(_make_ingredient(42, 5.0))

        fetch.assert_called_once_with(42)

    def test_fetch_exception_returns_as_is(self):
        """If fetch raises, ingredient is returned without lots."""
        def bad_fetch(id_mp):
            raise ConnectionError("API down")

        tracker = BatchLotTracker(fetch_lots_fn=bad_fetch)
        ing = _make_ingredient(42, 10.0)
        result = tracker.distribute_ingredient(ing)
        assert result == [ing]
