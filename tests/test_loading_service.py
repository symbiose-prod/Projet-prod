"""Tests des fonctions pures de common.services.loading_service.

Pas de DB ni de NiceGUI ici — uniquement la logique de normalisation
SSCC et d'agrégation des palettes en lignes ramasse.
"""
from __future__ import annotations

import datetime as _dt
from unittest import mock

import pytest

from common.services import loading_service
from common.services.loading_service import (
    PaletteInfo,
    _normalize_sscc,
    aggregate_palettes_to_lines,
    list_linked_palettes,
    list_palettes_in_cold_room,
    rebuild_lines_from_palettes,
    unlink_palette,
)

# ─── _normalize_sscc ────────────────────────────────────────────────────────

class TestNormalizeSscc:

    def test_clean_18_digits(self):
        assert _normalize_sscc("337700144200000005") == "337700144200000005"

    def test_strips_ai_prefix(self):
        # AI (00) + 18 digits → 20 digits, on garde les 18 derniers
        assert _normalize_sscc("(00)337700144200000005") == "337700144200000005"

    def test_strips_spaces(self):
        assert _normalize_sscc("3377 0014 4200 0000 05") == "337700144200000005"

    def test_strips_dashes(self):
        assert _normalize_sscc("3377-0014-4200-0000-05") == "337700144200000005"

    def test_empty(self):
        assert _normalize_sscc("") == ""
        assert _normalize_sscc(None) == ""  # type: ignore[arg-type]

    def test_invalid_keeps_digits(self):
        # Si moins de 18 digits, on retourne ce qu'on a (validation se fait au call site)
        assert _normalize_sscc("123") == "123"

    def test_ai_prefix_without_leading_00(self):
        # 18 digits sans AI prefix, ne pas confondre
        assert _normalize_sscc("123456789012345678") == "123456789012345678"


# ─── aggregate_palettes_to_lines ────────────────────────────────────────────

def _fake_carton_weight(fmt: str, designation: str) -> float:
    """Mock injectable — poids constant pour les tests."""
    weights = {"12x33": 6.741, "6x33": 3.42, "6x75": 7.23, "4x75": 5.85}
    return weights.get(fmt.lower(), 0.0)


def _palette(
    sscc="3" + "3" * 17, fmt="12x33", designation="Kéfir Test", case_count=126,
    lot="L001", gtin_palette="03770014427250", ddm=None,
) -> PaletteInfo:
    return PaletteInfo(
        sscc=sscc, gtin_palette=gtin_palette, lot=lot,
        ddm=ddm or _dt.date(2027, 5, 1),
        case_count=case_count, designation=designation, fmt=fmt,
        marque="SYM", gout="Test", pcb=12, gtin_uvc="",
        generated_at=_dt.datetime(2026, 5, 13, 14, 0),
    )


class TestAggregate:

    def test_single_palette(self):
        out = aggregate_palettes_to_lines(
            [_palette()], carton_weight_fn=_fake_carton_weight,
        )
        assert len(out) == 1
        line = out[0]
        assert line["palettes"] == 1
        assert line["cartons"] == 126
        # 126 × 6.741 + 1 × 25 = 849.4 + 25 ≈ 874
        assert line["poids"] == round(126 * 6.741 + 25)
        assert line["produit"] == "Kéfir Test 12x33"

    def test_two_palettes_same_product(self):
        out = aggregate_palettes_to_lines(
            [_palette(), _palette()],
            carton_weight_fn=_fake_carton_weight,
        )
        assert len(out) == 1
        line = out[0]
        assert line["palettes"] == 2
        assert line["cartons"] == 252
        # 252 × 6.741 + 2 × 25 = 1698.7 + 50 ≈ 1749
        assert line["poids"] == round(252 * 6.741 + 50)

    def test_two_different_products(self):
        out = aggregate_palettes_to_lines([
            _palette(designation="Kéfir Pêche", fmt="6x75", case_count=84),
            _palette(designation="Kéfir Gingembre", fmt="6x33", case_count=216),
        ], carton_weight_fn=_fake_carton_weight)
        assert len(out) == 2
        # Tri alphabétique par produit
        assert "Gingembre" in out[0]["produit"]
        assert "Pêche" in out[1]["produit"]

    def test_partial_palettes_aggregated(self):
        # 1 palette pleine (126) + 1 partielle (50) du même produit = 176 cartons
        out = aggregate_palettes_to_lines([
            _palette(case_count=126),
            _palette(case_count=50, sscc="3" + "4" * 17),
        ], carton_weight_fn=_fake_carton_weight)
        assert len(out) == 1
        assert out[0]["cartons"] == 176
        assert out[0]["palettes"] == 2

    def test_empty_returns_empty(self):
        assert aggregate_palettes_to_lines([], carton_weight_fn=_fake_carton_weight) == []

    def test_ddm_uses_earliest(self):
        # Quand plusieurs palettes ont des DDM différentes, on garde la plus proche
        out = aggregate_palettes_to_lines([
            _palette(ddm=_dt.date(2027, 6, 1)),
            _palette(ddm=_dt.date(2027, 5, 1), sscc="3" + "4" * 17),
            _palette(ddm=_dt.date(2027, 7, 1), sscc="3" + "5" * 17),
        ], carton_weight_fn=_fake_carton_weight)
        assert len(out) == 1
        assert out[0]["ddm"] == "01/05/2027"

    def test_no_carton_weight_function(self):
        # Si get_carton_weight retourne 0 (format inconnu), poids = 0 + 25 par palette
        out = aggregate_palettes_to_lines([
            _palette(fmt="invalid_fmt"),
        ], carton_weight_fn=_fake_carton_weight)
        assert out[0]["poids"] == 25  # juste le poids de la palette vide

    def test_unknown_format_returns_invalid(self):
        with pytest.raises((ValueError, TypeError, KeyError)):
            # Stress test : si carton_weight_fn lève, on propage
            def boom(fmt, d):
                raise ValueError("fmt inconnu")
            aggregate_palettes_to_lines([_palette()], carton_weight_fn=boom)


# ─── rebuild_lines_from_palettes ────────────────────────────────────────────

def _db_row(
    sscc="3" + "3" * 17, fmt="12x33", designation="Kéfir Test", case_count=126,
    lot="L001", gtin_palette="03770014427250",
    ddm=_dt.date(2027, 5, 1),
):
    """Forge une row telle que run_sql() la retourne — keys alignés sur
    le SELECT de rebuild_lines_from_palettes."""
    return {
        "sscc": sscc, "gtin_palette": gtin_palette, "lot": lot, "ddm": ddm,
        "case_count": case_count, "generated_at": _dt.datetime(2026, 5, 13, 14, 0),
        "designation": designation, "fmt": fmt, "marque": "SYM", "gout": "Test",
        "pcb": 12, "gtin_uvc": "",
    }


class TestRebuildLinesFromPalettes:

    def test_no_palettes_returns_empty(self):
        with mock.patch.object(loading_service, "run_sql", return_value=[]):
            lines, tc, tp, tw = rebuild_lines_from_palettes(
                "rid-1", "tid-1", carton_weight_fn=_fake_carton_weight,
            )
        assert lines == []
        assert (tc, tp, tw) == (0, 0, 0)

    def test_single_palette_aggregated(self):
        with mock.patch.object(loading_service, "run_sql", return_value=[_db_row()]):
            lines, tc, tp, tw = rebuild_lines_from_palettes(
                "rid-1", "tid-1", carton_weight_fn=_fake_carton_weight,
            )
        assert len(lines) == 1
        assert tc == 126
        assert tp == 1
        # 126 × 6.741 + 1 × 25 = 849.4 + 25 ≈ 874
        assert tw == round(126 * 6.741 + 25)

    def test_two_palettes_same_product_sum(self):
        # Deux palettes du même produit → une seule ligne avec totaux cumulés
        rows = [_db_row(), _db_row(sscc="3" + "4" * 17)]
        with mock.patch.object(loading_service, "run_sql", return_value=rows):
            lines, tc, tp, tw = rebuild_lines_from_palettes(
                "rid-1", "tid-1", carton_weight_fn=_fake_carton_weight,
            )
        assert len(lines) == 1
        assert tc == 252
        assert tp == 2

    def test_two_different_products_two_lines(self):
        rows = [
            _db_row(designation="Kéfir Pêche", fmt="6x75", case_count=84),
            _db_row(
                designation="Kéfir Gingembre", fmt="6x33", case_count=216,
                sscc="3" + "4" * 17,
            ),
        ]
        with mock.patch.object(loading_service, "run_sql", return_value=rows):
            lines, tc, tp, tw = rebuild_lines_from_palettes(
                "rid-1", "tid-1", carton_weight_fn=_fake_carton_weight,
            )
        assert len(lines) == 2
        assert tc == 300  # 84 + 216
        assert tp == 2

    def test_invalid_row_skipped_without_raising(self):
        # Row avec ddm en string mal formé → ignorée, on continue avec les bonnes
        good = _db_row()
        bad = _db_row(sscc="3" + "5" * 17)
        bad["ddm"] = "not-a-date"  # ValueError dans fromisoformat
        with mock.patch.object(loading_service, "run_sql", return_value=[good, bad]):
            lines, tc, tp, tw = rebuild_lines_from_palettes(
                "rid-1", "tid-1", carton_weight_fn=_fake_carton_weight,
            )
        # Seule la bonne row a été agrégée
        assert tp == 1
        assert tc == 126

    def test_sql_filters_active_only(self):
        # Vérifie que la query filtre bien sur unlinked_at IS NULL et voided_at IS NULL
        # (test de régression contre l'ancienne logique additive).
        captured = {}

        def fake_run_sql(sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return []

        with mock.patch.object(loading_service, "run_sql", side_effect=fake_run_sql):
            rebuild_lines_from_palettes(
                "rid-42", "tid-7", carton_weight_fn=_fake_carton_weight,
            )

        sql = captured["sql"]
        # Garde-fous : si quelqu'un retire un de ces filtres, ces assertions sautent
        assert "pl.unlinked_at  IS NULL" in sql or "pl.unlinked_at IS NULL" in sql
        assert "sl.voided_at    IS NULL" in sql or "sl.voided_at IS NULL" in sql
        assert "eph.designation IS NOT NULL" in sql
        assert captured["params"] == {"rid": "rid-42", "t": "tid-7"}


# ─── list_linked_palettes ───────────────────────────────────────────────────

class TestListLinkedPalettes:
    """Le helper qui sert à la fois à rebuild et à l'UI de déliage."""

    def test_empty_when_no_rows(self):
        with mock.patch.object(loading_service, "run_sql", return_value=[]):
            out = list_linked_palettes("rid-1", "tid-1")
        assert out == []

    def test_single_row_typed(self):
        with mock.patch.object(loading_service, "run_sql", return_value=[_db_row()]):
            out = list_linked_palettes("rid-1", "tid-1")
        assert len(out) == 1
        # PaletteInfo bien construit
        assert out[0].sscc == "3" + "3" * 17
        assert out[0].case_count == 126
        assert out[0].fmt == "12x33"

    def test_invalid_row_skipped(self):
        good = _db_row()
        bad = _db_row(sscc="3" + "5" * 17)
        bad["ddm"] = "not-a-date"
        with mock.patch.object(loading_service, "run_sql", return_value=[good, bad]):
            out = list_linked_palettes("rid-1", "tid-1")
        assert len(out) == 1  # bad ignorée


# ─── unlink_palette ─────────────────────────────────────────────────────────

class TestUnlinkPalette:

    def test_returns_true_when_link_unlinked(self):
        with mock.patch.object(loading_service, "run_sql", return_value=[{"id": 42}]):
            ok = unlink_palette(
                "tid-1",
                sscc="3" + "3" * 17,
                ramasse_id="rid-1",
                reason="Palette cassée au chargement",
                user_email="op@example.com",
            )
        assert ok is True

    def test_returns_false_when_no_active_link(self):
        # Pas de row matchée : palette pas liée à cette ramasse, ou déjà unlinked
        with mock.patch.object(loading_service, "run_sql", return_value=[]):
            ok = unlink_palette(
                "tid-1",
                sscc="3" + "3" * 17,
                ramasse_id="rid-1",
                reason="Test",
            )
        assert ok is False

    def test_invalid_sscc_returns_false_without_query(self):
        with mock.patch.object(
            loading_service, "run_sql", return_value=[{"id": 1}],
        ) as mock_run:
            ok = unlink_palette(
                "tid-1", sscc="not-a-sscc",
                ramasse_id="rid-1", reason="Test",
            )
        assert ok is False
        mock_run.assert_not_called()

    def test_sql_targets_active_link_only(self):
        # Garde-fou : la query DOIT inclure unlinked_at IS NULL pour ne pas
        # réécrire la raison d'un unlink antérieur.
        captured = {}

        def fake(sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return [{"id": 1}]

        with mock.patch.object(loading_service, "run_sql", side_effect=fake):
            unlink_palette(
                "tenant-99",
                sscc="3" + "7" * 17,
                ramasse_id="rid-999",
                reason="Erreur scan",
                user_email="alice@ferment.test",
            )

        sql = captured["sql"]
        params = captured["params"]
        assert "UPDATE palette_loadings" in sql
        assert "unlinked_at     = now()" in sql
        assert "unlinked_at IS NULL" in sql  # WHERE actif
        assert "ramasse_id  = :rid" in sql  # garde-fou ramasse
        assert "tenant_id   = :t" in sql    # tenant scoping
        assert params["sscc"] == "3" + "7" * 17
        assert params["t"] == "tenant-99"
        assert params["rid"] == "rid-999"
        assert params["u"] == "alice@ferment.test"
        assert params["r"] == "Erreur scan"

    def test_empty_reason_falls_back_to_default(self):
        captured = {}

        def fake(sql, params):
            captured["params"] = params
            return [{"id": 1}]

        with mock.patch.object(loading_service, "run_sql", side_effect=fake):
            unlink_palette(
                "tid-1", sscc="3" + "3" * 17, ramasse_id="rid-1", reason="   ",
            )
        # Une raison vide est remplacée par un fallback pour garder une
        # trace audit non vide.
        assert captured["params"]["r"] == "Sans raison précisée"

    def test_reason_capped_at_500_chars(self):
        captured = {}

        def fake(sql, params):
            captured["params"] = params
            return [{"id": 1}]

        long_reason = "X" * 1000
        with mock.patch.object(loading_service, "run_sql", side_effect=fake):
            unlink_palette(
                "tid-1", sscc="3" + "3" * 17, ramasse_id="rid-1",
                reason=long_reason,
            )
        assert len(captured["params"]["r"]) == 500


# ─── list_palettes_in_cold_room ─────────────────────────────────────────────

class TestListPalettesInColdRoom:
    """La requête source du « snapshot CF » pour la ramasse provisoire."""

    def test_empty_when_no_rows(self):
        with mock.patch.object(loading_service, "run_sql", return_value=[]):
            out = list_palettes_in_cold_room("tid-1")
        assert out == []

    def test_returns_typed_palettes(self):
        with mock.patch.object(loading_service, "run_sql", return_value=[_db_row()]):
            out = list_palettes_in_cold_room("tid-1")
        assert len(out) == 1
        assert out[0].designation == "Kéfir Test"
        assert out[0].case_count == 126

    def test_sql_filters_active_unloaded_palettes(self):
        # Garde-fou : on ne ramène ni les SSCC annulés, ni les palettes
        # déjà liées à une ramasse active.
        captured = {}

        def fake(sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return []

        with mock.patch.object(loading_service, "run_sql", side_effect=fake):
            list_palettes_in_cold_room("tid-99")

        sql = captured["sql"]
        params = captured["params"]
        # Filtre voided
        assert "sl.voided_at IS NULL" in sql
        # JOIN avec restriction sur liaison active + filtre pas-liée
        assert "pl.unlinked_at IS NULL" in sql
        assert "pl.id IS NULL" in sql
        # Designation obligatoire (pas d'anomalie DB qui passe en silence)
        assert "eph.designation IS NOT NULL" in sql
        # FIFO (ascendant) — DDM la plus proche en haut visuellement
        assert "ORDER BY sl.generated_at ASC" in sql
        # Tenant scoping
        assert params == {"t": "tid-99"}

    def test_invalid_row_skipped(self):
        good = _db_row()
        bad = _db_row(sscc="3" + "9" * 17)
        bad["ddm"] = "not-a-date"  # ValueError dans fromisoformat
        with mock.patch.object(
            loading_service, "run_sql", return_value=[good, bad],
        ):
            out = list_palettes_in_cold_room("tid-1")
        assert len(out) == 1
