"""Tests for commercial objectives tracking — pure logic, no API."""
from __future__ import annotations

from pages._commercial_calc import compute_objective_progress


class TestComputeObjectiveProgress:
    """Tests pour compute_objective_progress (calcul d'avancement pur)."""

    def test_zero_target_returns_zero(self):
        result = compute_objective_progress(ca_ref_total=0, ca_realized=0, target_delta=0)
        assert result["target"] == 0
        assert result["progress_pct"] == 0.0

    def test_basic_progress(self):
        # CA 2025 = 100k, objectif delta = +50k → target = 150k
        # CA 2026 réalisé = 75k → progress = 50%
        result = compute_objective_progress(
            ca_ref_total=100_000,
            ca_realized=75_000,
            target_delta=50_000,
        )
        assert result["target"] == 150_000
        assert result["progress_pct"] == 50.0

    def test_exceeded_target(self):
        # CA 2025 = 100k, delta = +50k → target = 150k
        # CA 2026 réalisé = 200k → 133.3%
        result = compute_objective_progress(
            ca_ref_total=100_000,
            ca_realized=200_000,
            target_delta=50_000,
        )
        assert result["target"] == 150_000
        assert result["progress_pct"] == 133.3

    def test_no_ref_ca(self):
        # Nouveau client (CA 2025 = 0), objectif = +25k → target = 25k
        # CA 2026 réalisé = 10k → 40%
        result = compute_objective_progress(
            ca_ref_total=0,
            ca_realized=10_000,
            target_delta=25_000,
        )
        assert result["target"] == 25_000
        assert result["progress_pct"] == 40.0

    def test_biocoop_scenario(self):
        """Scénario réel Biocoop : CA 2025 ~360k, delta +190k → target 550k."""
        result = compute_objective_progress(
            ca_ref_total=360_000,
            ca_realized=280_000,
            target_delta=190_000,
        )
        assert result["target"] == 550_000
        # 280k / 550k = 50.9%
        assert result["progress_pct"] == 50.9

    def test_niko_scenario(self):
        """Scénario Niko : pas de ventilation, objectif global +600k."""
        result = compute_objective_progress(
            ca_ref_total=0,
            ca_realized=150_000,
            target_delta=600_000,
        )
        assert result["target"] == 600_000
        assert result["progress_pct"] == 25.0

    def test_rounding(self):
        result = compute_objective_progress(
            ca_ref_total=100_000,
            ca_realized=33_333,
            target_delta=50_000,
        )
        assert result["target"] == 150_000
        # 33333 / 150000 = 22.222% → arrondi 22.2
        assert result["progress_pct"] == 22.2


class TestConfigLoading:
    """Vérifie que la config commercial se charge correctement."""

    def test_get_commercial_config(self):
        from common.data import get_commercial_config

        cfg = get_commercial_config()
        obj = cfg.get("objectives", {})
        assert obj["year"] == 2026
        assert obj["year_ref"] == 2025
        assert len(obj["brands"]) == 2

        # Symbiose
        symbiose = obj["brands"][0]
        assert symbiose["tag"] == "SYMBIOSE"
        assert symbiose["target_delta"] == 400_000
        assert len(symbiose["enseignes"]) == 7

        # Vérifier les enseignes
        tags = [e["tag"] for e in symbiose["enseignes"]]
        assert "BIOCOOP" in tags
        assert "LVC" in tags
        assert "RELAISVERT" in tags

        # Total deltas Symbiose = 400k
        total_delta = sum(e["target_delta"] for e in symbiose["enseignes"])
        assert total_delta == 400_000

        # Niko
        niko = obj["brands"][1]
        assert niko["tag"] == "NIKO"
        assert niko["target_delta"] == 600_000
        assert niko["enseignes"] == []
