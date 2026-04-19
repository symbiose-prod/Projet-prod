"""Tests for common/ramasse_export — pure logic, no DB."""
from __future__ import annotations

from datetime import date, datetime

from common.ramasse_export import build_csv_bytes, filename_for_month, month_bounds


class TestBuildCsvBytes:

    def test_header_row(self):
        out = build_csv_bytes([]).decode("utf-8")
        assert out.startswith("\ufeff")  # BOM
        # Retire le BOM pour l'assertion sur le header
        assert "Date;Destinataire;Version;Lignes;Cartons;Palettes" in out

    def test_single_row(self):
        items = [{
            "date_ramasse": date(2026, 4, 19),
            "destinataire": "SOFRIPA Lyon",
            "version": 2,
            "line_count": 12,
            "total_cartons": 150,
            "total_palettes": 4,
            "total_poids_kg": 1200,
            "driver_passed": True,
            "deleted_at": None,
            "created_at": datetime(2026, 4, 18, 14, 30),
        }]
        out = build_csv_bytes(items).decode("utf-8")
        lines = out.splitlines()
        assert len(lines) == 2
        # accents encoded correctly (SOFRIPA ok, Destinataire header above)
        assert "SOFRIPA Lyon" in lines[1]
        assert "2026-04-19" in lines[1]
        assert "Oui" in lines[1]  # driver_passed
        assert "Non" in lines[1]  # deleted_at (None)

    def test_missing_fields_default(self):
        items = [{}]
        out = build_csv_bytes(items).decode("utf-8")
        # Pas de crash sur champs manquants
        lines = out.splitlines()
        assert len(lines) == 2
        assert lines[1].count(";") == 9  # 10 colonnes = 9 séparateurs

    def test_deleted_at_reflected(self):
        items = [{
            "date_ramasse": date(2026, 4, 1),
            "destinataire": "X",
            "deleted_at": datetime(2026, 4, 5, 10, 0),
        }]
        out = build_csv_bytes(items).decode("utf-8")
        assert "Oui" in out.splitlines()[1]  # colonne Supprimée

    def test_semicolon_delimiter_excel_compat(self):
        items = [{
            "date_ramasse": date(2026, 4, 1),
            "destinataire": "Client, avec virgule",
        }]
        out = build_csv_bytes(items).decode("utf-8")
        # Valeur avec virgule = pas de quote car séparateur = ';'
        assert "Client, avec virgule" in out


class TestFilenameForMonth:
    def test_format(self):
        assert filename_for_month(2026, 4) == "ramasses_2026-04.csv"
        assert filename_for_month(2026, 12) == "ramasses_2026-12.csv"


class TestMonthBounds:
    def test_normal_month(self):
        first, last = month_bounds(2026, 4)
        assert first == date(2026, 4, 1)
        assert last == date(2026, 4, 30)

    def test_february_leap(self):
        first, last = month_bounds(2024, 2)  # bissextile
        assert last == date(2024, 2, 29)

    def test_february_non_leap(self):
        first, last = month_bounds(2026, 2)
        assert last == date(2026, 2, 28)

    def test_december(self):
        first, last = month_bounds(2025, 12)
        assert last == date(2025, 12, 31)
