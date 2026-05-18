"""Tests de common.services.cuve_service — registre cuves + calibration."""
from __future__ import annotations

from common.services.cuve_service import get_cuves


class TestGetCuves:
    def test_returns_cuves_and_calibration(self):
        result = get_cuves()
        assert "cuves" in result
        assert "calibration" in result

    def test_cuves_have_nom_and_capacite(self):
        cuves = get_cuves()["cuves"]
        assert len(cuves) >= 3
        noms = {c["nom"] for c in cuves}
        assert {"Cuve 1", "Cuve 2", "Cuve 3"} <= noms
        for c in cuves:
            assert isinstance(c["capacite_l"], int)

    def test_cuves_1_2_partagent_la_capacite_5200(self):
        by_nom = {c["nom"]: c["capacite_l"] for c in get_cuves()["cuves"]}
        assert by_nom["Cuve 1"] == 5200
        assert by_nom["Cuve 2"] == 5200
        assert by_nom["Cuve 3"] == 7200

    def test_calibration_keyed_by_capacity(self):
        calib = get_cuves()["calibration"]
        assert "5200" in calib
        assert "7200" in calib

    def test_calibration_points_sorted_by_volume(self):
        for points in get_cuves()["calibration"].values():
            volumes = [p["volume_l"] for p in points]
            assert volumes == sorted(volumes)

    def test_calibration_point_shape(self):
        points = get_cuves()["calibration"]["7200"]
        assert points
        assert "volume_l" in points[0]
        assert "hauteur_cm" in points[0]
