"""Tests for common/lot_fifo.py — FIFO lot allocation."""
from __future__ import annotations

from common.lot_fifo import LotPool


def _make_lot(lot_id: int, qty: float, dluo: str = "2026-06-01") -> dict:
    return {
        "idMatierePremiereNumeroLot": lot_id,
        "quantite": qty,
        "dateLimiteUtilisationOptimale": dluo,
        "numeroLot": f"LOT-{lot_id}",
    }


# ─── LotPool.allocate ────────────────────────────────────────────────────────

class TestLotPoolAllocate:

    def test_exact_allocation_single_lot(self):
        pool = LotPool(1, [_make_lot(100, 50.0)])
        result = pool.allocate(50.0)
        assert len(result) == 1
        assert result[0]["quantite"] == 50.0
        assert result[0]["code"] == "LOT-100"

    def test_partial_from_first_lot(self):
        pool = LotPool(1, [_make_lot(100, 50.0)])
        result = pool.allocate(20.0)
        assert len(result) == 1
        assert result[0]["quantite"] == 20.0

    def test_split_across_two_lots_fifo(self):
        lots = [
            _make_lot(100, 30.0, "2026-03-01"),  # oldest → used first
            _make_lot(200, 50.0, "2026-06-01"),
        ]
        pool = LotPool(1, lots)
        result = pool.allocate(40.0)
        assert len(result) == 2
        assert result[0]["quantite"] == 30.0  # all of lot 100
        assert result[1]["quantite"] == 10.0  # 10 from lot 200

    def test_fifo_order_by_dluo(self):
        lots = [
            _make_lot(200, 20.0, "2026-12-01"),  # newer
            _make_lot(100, 20.0, "2026-01-01"),  # older → should be first
        ]
        pool = LotPool(1, lots)
        result = pool.allocate(10.0)
        assert result[0]["code"] == "LOT-100"

    def test_over_allocation_returns_what_available(self):
        pool = LotPool(1, [_make_lot(100, 10.0)])
        result = pool.allocate(50.0)
        assert len(result) == 1
        assert result[0]["quantite"] == 10.0

    def test_zero_needed(self):
        pool = LotPool(1, [_make_lot(100, 50.0)])
        result = pool.allocate(0)
        assert result == []

    def test_negative_needed(self):
        pool = LotPool(1, [_make_lot(100, 50.0)])
        result = pool.allocate(-5.0)
        assert result == []

    def test_empty_lots(self):
        pool = LotPool(1, [])
        assert pool.has_lots is False
        result = pool.allocate(10.0)
        assert result == []

    def test_zero_qty_lots_filtered(self):
        lots = [_make_lot(100, 0.0), _make_lot(200, 10.0)]
        pool = LotPool(1, lots)
        result = pool.allocate(5.0)
        assert len(result) == 1
        assert result[0]["code"] == "LOT-200"

    def test_successive_allocations_consume_stock(self):
        pool = LotPool(1, [_make_lot(100, 20.0)])
        r1 = pool.allocate(15.0)
        assert r1[0]["quantite"] == 15.0
        r2 = pool.allocate(10.0)
        assert r2[0]["quantite"] == 5.0  # only 5 remaining

    def test_id_matiere_premiere_set(self):
        pool = LotPool(42, [_make_lot(100, 10.0)])
        result = pool.allocate(5.0)
        assert result[0]["idMatierePremiere"] == 42
