"""Tests des fonctions de common.services.production_sheet_service.

Mock SQL via run_sql — on teste la logique de transformation, pas la DB.
"""
from __future__ import annotations

import datetime as _dt
from unittest import mock

import pytest

from common.services import production_sheet_service
from common.services.production_sheet_service import (
    ConditionnementByLot,
    ConditionnementLine,
    ProductionSheetDetail,
    compute_real_conditionnement_by_lot,
    finalize_sheet,
)

# ─── compute_real_conditionnement_by_lot ───────────────────────────────────

class TestComputeRealConditionnementByLot:

    def test_empty_lot_returns_empty_without_query(self):
        # On ne doit même pas appeler run_sql si le lot est vide
        with mock.patch.object(
            production_sheet_service, "run_sql",
        ) as mock_sql:
            result = compute_real_conditionnement_by_lot("tenant-A", "")
        mock_sql.assert_not_called()
        assert result.lot == ""
        assert result.items == []
        assert result.total_cartons == 0
        assert result.total_palettes == 0

    def test_whitespace_only_lot_returns_empty(self):
        with mock.patch.object(
            production_sheet_service, "run_sql",
        ) as mock_sql:
            result = compute_real_conditionnement_by_lot("tenant-A", "   ")
        mock_sql.assert_not_called()
        assert result.items == []

    def test_aggregates_rows_into_lines(self):
        # 2 lignes agrégées par (fmt, marque) en sortie SQL
        fake_rows = [
            {
                "fmt": "12x33", "marque": "SYMBIOSE",
                "designation": "K. Mangue - Passion",
                "total_cartons": 843, "total_palettes": 12,
            },
            {
                "fmt": "6x75", "marque": "SYMBIOSE",
                "designation": "K. Mangue - Passion",
                "total_cartons": 347, "total_palettes": 4,
            },
        ]
        with mock.patch.object(
            production_sheet_service, "run_sql", return_value=fake_rows,
        ) as mock_sql:
            result = compute_real_conditionnement_by_lot("tenant-A", "15052027")
        # SQL appelé avec les bons params
        assert mock_sql.call_args[0][1] == {
            "tid": "tenant-A", "lot": "15052027",
        }
        # Output
        assert isinstance(result, ConditionnementByLot)
        assert result.lot == "15052027"
        assert len(result.items) == 2
        assert result.items[0] == ConditionnementLine(
            fmt="12x33", marque="SYMBIOSE",
            designation="K. Mangue - Passion",
            cartons=843, palettes=12,
        )
        assert result.total_cartons == 843 + 347
        assert result.total_palettes == 12 + 4

    def test_no_rows_returns_zero_totals(self):
        # Lot inconnu / aucune palette étiquetée → totaux à 0
        with mock.patch.object(
            production_sheet_service, "run_sql", return_value=[],
        ):
            result = compute_real_conditionnement_by_lot("tenant-A", "ZZZ")
        assert result.items == []
        assert result.total_cartons == 0
        assert result.total_palettes == 0

    def test_handles_null_values_defensively(self):
        # case_count ou marque NULL dans la DB → on coerce sans crasher
        fake_rows = [
            {
                "fmt": None, "marque": None, "designation": None,
                "total_cartons": None, "total_palettes": 3,
            },
        ]
        with mock.patch.object(
            production_sheet_service, "run_sql", return_value=fake_rows,
        ):
            result = compute_real_conditionnement_by_lot("tenant-A", "L")
        assert result.items[0].fmt == ""
        assert result.items[0].marque == ""
        assert result.items[0].cartons == 0
        assert result.items[0].palettes == 3

    def test_lot_is_trimmed_before_query(self):
        with mock.patch.object(
            production_sheet_service, "run_sql", return_value=[],
        ) as mock_sql:
            compute_real_conditionnement_by_lot("tenant-A", "  15052027  ")
        # Le lot transmis à SQL est trimmé
        assert mock_sql.call_args[0][1]["lot"] == "15052027"


# ─── finalize_sheet (Sprint 4) ─────────────────────────────────────────────

def _make_sheet(**kwargs) -> ProductionSheetDetail:
    """Construit un ProductionSheetDetail pour les tests finalize."""
    defaults = dict(
        id="sheet-1",
        brassin_id="42",
        produit="K. Mangue - Passion",
        cuve="Cuve de 7200L",
        ddm=_dt.date(2027, 5, 15),
        lot="15052027",
        status="draft",
        data={
            "fermentation": {"mesures": [], "statut": ""},
            "dilution": {"sucre_kg": 252.0, "figues_kg": 115.2},
            "remarques": "RAS",
        },
        created_at=_dt.datetime(2026, 5, 15, 10, 0, tzinfo=_dt.UTC),
        updated_at=_dt.datetime(2026, 5, 17, 15, 0, tzinfo=_dt.UTC),
        finalized_at=None,
        created_by_email="nicolas@symbiose-kefir.fr",
    )
    defaults.update(kwargs)
    return ProductionSheetDetail(**defaults)


class TestFinalizeSheet:

    def test_sheet_not_found_raises(self):
        with mock.patch.object(
            production_sheet_service, "get_sheet", return_value=None,
        ):
            with pytest.raises(ValueError, match="not found"):
                finalize_sheet("tenant-A", "missing")

    def test_already_completed_raises(self):
        sheet = _make_sheet(status="completed")
        with mock.patch.object(
            production_sheet_service, "get_sheet", return_value=sheet,
        ):
            with pytest.raises(ValueError, match="already finalized"):
                finalize_sheet("tenant-A", "sheet-1")

    def test_happy_path_generates_pdf_and_updates(self):
        sheet = _make_sheet(status="draft")
        finalized = _make_sheet(
            status="completed",
            finalized_at=_dt.datetime(2026, 5, 17, 18, 0, tzinfo=_dt.UTC),
        )
        # get_sheet est appelé 2 fois : pré-check + reload après UPDATE
        get_calls = [sheet, finalized]
        with (
            mock.patch.object(
                production_sheet_service, "get_sheet",
                side_effect=lambda *_args, **_kw: get_calls.pop(0),
            ),
            mock.patch.object(
                production_sheet_service, "run_sql",
                return_value=[{"id": "sheet-1"}],
            ) as mock_sql,
            mock.patch(
                "common.production_sheet_pdf.build_production_sheet_pdf",
                return_value=b"%PDF-fake",
            ) as mock_build,
        ):
            updated, pdf = finalize_sheet(
                "tenant-A", "sheet-1", user_email="op@sym.fr",
            )
        # PDF builder appelé avec la fiche pré-finalize
        assert mock_build.call_args[0][0] is sheet
        # UPDATE SQL appelé avec status='draft' (refus si déjà completed)
        # via le WHERE status='draft' du service
        sql_call = mock_sql.call_args
        assert "completed" in sql_call[0][0]  # SET status='completed'
        assert sql_call[0][1]["pdf"] == b"%PDF-fake"
        # Retour : version mise à jour + PDF bytes
        assert updated.status == "completed"
        assert pdf == b"%PDF-fake"

    def test_race_condition_returns_value_error(self):
        """Si entre le get et l'UPDATE quelqu'un a déjà finalisé,
        l'UPDATE renvoie 0 ligne → ValueError."""
        sheet = _make_sheet(status="draft")
        with (
            mock.patch.object(
                production_sheet_service, "get_sheet", return_value=sheet,
            ),
            mock.patch.object(
                production_sheet_service, "run_sql", return_value=[],
            ),
            mock.patch(
                "common.production_sheet_pdf.build_production_sheet_pdf",
                return_value=b"%PDF",
            ),
        ):
            with pytest.raises(ValueError, match="draft"):
                finalize_sheet("tenant-A", "sheet-1")


# ─── PDF builder smoke test (vérifie qu'il produit du PDF valide) ──────────

class TestPDFBuilder:
    """Smoke test : on construit un PDF complet et on vérifie qu'on a
    bien un blob qui commence par %PDF. Pas de validation visuelle —
    on s'assure juste que la fonction ne crashe pas sur des inputs réels.
    """

    def test_minimal_sheet_produces_pdf(self):
        from common.production_sheet_pdf import build_production_sheet_pdf

        sheet = _make_sheet()
        pdf = build_production_sheet_pdf(sheet)
        assert isinstance(pdf, bytes)
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 1000  # PDF non-trivial

    def test_sheet_with_full_data_produces_pdf(self):
        from common.production_sheet_pdf import build_production_sheet_pdf

        sheet = _make_sheet(data={
            "fermentation": {
                "mesures": [
                    {"date": "2026-05-15", "heure": "10:00",
                     "brix": 12.5, "ph": 3.8, "temperature": 24.0,
                     "gout": "OK", "observation": "Démarrage propre",
                     "matricule": "NP"},
                    {"date": "2026-05-16", "heure": "10:00",
                     "brix": 8.2, "ph": 3.5, "temperature": 22.0,
                     "gout": "OK", "observation": "",
                     "matricule": "NP"},
                ],
                "statut": "Conforme",
            },
            "dilution": {
                "sucre_kg": 252.0, "figues_kg": 115.2,
                "jus_citron_kg": 28.8, "grains_kg": 43.2,
                "volume_remplissage_l": 7200.0,
                "niveau_liquide_cm": 249.0,
                "pression_bulleur_bars": 3.0,
                "temperature_cuve_c": 24.0,
            },
            "filtration": {
                "volume_filtre_l": 4080.0,
                "volume_final_l": 4325.0,
                "hauteur_cm": 153.0,
            },
            "remplissage": {
                "volume_total_l": 7045.0,
                "hauteur_cm": 243.8,
            },
            "conditionnement_prevu": {
                "items": [
                    {"id": "1", "fmt": "12x33", "marque": "SYMBIOSE",
                     "designation": "K. Mangue - Passion",
                     "cartons": 843, "palettes": 12},
                    {"id": "2", "fmt": "6x75", "marque": "SYMBIOSE",
                     "designation": "K. Mangue - Passion",
                     "cartons": 347, "palettes": 4},
                ],
            },
            "conditionnement_reel": {
                "items": [
                    {"id": "1", "fmt": "12x33", "marque": "SYMBIOSE",
                     "designation": "K. Mangue",
                     "cartons": 840, "palettes": 12},
                ],
                "sourced_from_sscc": True,
                "lot_used": "15052027",
                "fetched_at": "2026-05-17T18:00:00+00:00",
            },
            "repartition": {
                "antoine_cartons": 5,
                "echantillons_cartons": 12,
                "tracabilite_cartons": 3,
            },
            "remarques": "Rien à signaler. Bon brassin.",
            "incidents": {
                "notes": "Petit débordement à la dilution.",
                "photos": [],
            },
        })
        pdf = build_production_sheet_pdf(sheet)
        assert isinstance(pdf, bytes)
        assert pdf.startswith(b"%PDF")
        # Avec toutes les données, le PDF doit être plus gros
        assert len(pdf) > 2000

    def test_sheet_with_unicode_characters(self):
        """Les caractères accentués (é, à, °C, etc.) ne doivent pas crasher
        FPDF natif (limité à latin-1). Le helper _latin1 fait la conversion."""
        from common.production_sheet_pdf import build_production_sheet_pdf

        sheet = _make_sheet(
            produit="Kéfir Pamplemousse — édition spéciale",
            data={
                "remarques": "Brassage avec température élevée (28°C)…",
            },
        )
        pdf = build_production_sheet_pdf(sheet)
        assert pdf.startswith(b"%PDF")
