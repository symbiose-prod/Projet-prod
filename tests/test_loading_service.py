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
    _build_df_for_pdf,
    _normalize_sscc,
    aggregate_palettes_to_lines,
    list_linked_palettes,
    list_palettes_in_cold_room,
    palettes_to_detailed_lines,
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


class TestPalettesToDetailedLines:
    """Format détaillé Sofripa : 1 ligne par palette (SSCC unique)."""

    def _palette(self, sscc="3" + "0" * 17, lot="L001", designation="Kéfir Pêche", fmt="6x75"):
        return PaletteInfo(
            sscc=sscc, gtin_palette="03700123456789", lot=lot,
            ddm=_dt.date(2027, 5, 1), case_count=84,
            designation=designation, fmt=fmt, marque="SYM", gout="Peche",
            pcb=12, gtin_uvc="3700123456780",
            generated_at=_dt.datetime(2026, 5, 13, 14, 0),
        )

    def test_one_line_per_palette(self):
        p1 = self._palette()
        p2 = self._palette(sscc="3" + "1" * 17, lot="L002")
        # Même produit, SSCC différents → 2 lignes en mode détaillé
        lines = palettes_to_detailed_lines([p1, p2], carton_weight_fn=_fake_carton_weight)
        assert len(lines) == 2

    def test_required_keys_present(self):
        p = self._palette()
        lines = palettes_to_detailed_lines([p], carton_weight_fn=_fake_carton_weight)
        assert lines, "lines vide alors qu'on a une palette"
        line = lines[0]
        for key in ("ref", "sscc", "sofripa_label", "produit", "ddm", "lot", "cartons", "poids"):
            assert key in line, f"clé manquante : {key}"
        assert line["sscc"] == "3" + "0" * 17
        assert line["lot"] == "L001"
        assert line["cartons"] == 84

    def test_empty_input_returns_empty(self):
        assert palettes_to_detailed_lines([]) == []

    def test_build_df_detects_detailed_mode(self):
        # Avec sscc dans les lines → DataFrame avec colonne SSCC
        lines = [
            {
                "ref": "123456", "sscc": "3" + "0" * 17, "sofripa_label": None,
                "produit": "Kéfir Pêche 6x75", "ddm": "01/05/2027",
                "lot": "L001", "cartons": 84, "poids": 591,
            }
        ]
        df = _build_df_for_pdf(lines)
        assert "SSCC" in df.columns
        assert "Lot" in df.columns
        assert "Réf. Sofripa" in df.columns
        # SSCC tronqué aux 8 derniers digits
        assert str(df.iloc[0]["SSCC"]) == "0" * 8

    def test_build_df_legacy_mode(self):
        # Sans sscc → format legacy 6 colonnes
        lines = [
            {"ref": "123456", "produit": "Kéfir Pêche 6x75", "ddm": "01/05/2027",
             "cartons": 84, "palettes": 1, "poids": 591}
        ]
        df = _build_df_for_pdf(lines)
        assert "SSCC" not in df.columns
        assert "Nb palettes" in df.columns


class TestRebuildLinesFromPalettes:

    def test_no_palettes_returns_empty(self):
        with mock.patch.object(loading_service, "run_sql", return_value=[]):
            lines, tc, tp, tw = rebuild_lines_from_palettes(
                "rid-1", "tid-1", carton_weight_fn=_fake_carton_weight,
            )
        assert lines == []
        assert (tc, tp, tw) == (0, 0, 0)

    def test_single_palette_detailed(self):
        # Format détaillé Sofripa : 1 palette = 1 ligne, avec sscc + lot
        with mock.patch.object(loading_service, "run_sql", return_value=[_db_row()]):
            lines, tc, tp, tw = rebuild_lines_from_palettes(
                "rid-1", "tid-1", carton_weight_fn=_fake_carton_weight,
            )
        assert len(lines) == 1
        assert tc == 126
        assert tp == 1
        # 126 × 6.741 + 1 × 25 = 849.4 + 25 ≈ 874
        assert tw == round(126 * 6.741 + 25)
        # Vérifie présence des clés détaillées
        assert "sscc" in lines[0]
        assert "lot" in lines[0]
        assert lines[0]["cartons"] == 126

    def test_two_palettes_same_product_two_lines(self):
        # Format détaillé : 2 palettes même produit → 2 lignes (1 par SSCC)
        rows = [_db_row(), _db_row(sscc="3" + "4" * 17)]
        with mock.patch.object(loading_service, "run_sql", return_value=rows):
            lines, tc, tp, tw = rebuild_lines_from_palettes(
                "rid-1", "tid-1", carton_weight_fn=_fake_carton_weight,
            )
        # Format détaillé : 2 SSCC distincts = 2 lignes (pas d'agrégation)
        assert len(lines) == 2
        assert tc == 252
        assert tp == 2
        # Les 2 lignes ont des SSCC différents
        ssccs = {line["sscc"] for line in lines}
        assert len(ssccs) == 2

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
        # Seule la bonne row a survécu
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


# ─── normalize_packaging_payload ────────────────────────────────────────────

class TestNormalizePackagingPayload:
    """Tests de la normalisation packaging entrant (mobile/web)."""

    def test_keeps_valid_items(self):
        from common.services.loading_service import normalize_packaging_payload

        result = normalize_packaging_payload([
            {"label": "Palette Bouteilles 33cl", "qty": 2, "unit": "palette"},
            {"label": "Palette Bouteilles 75cl", "qty": 1, "unit": "palette"},
        ])
        assert len(result) == 2
        assert result[0] == {
            "label": "Palette Bouteilles 33cl", "qty": 2, "unit": "palette",
        }

    def test_filters_zero_qty(self):
        from common.services.loading_service import normalize_packaging_payload

        result = normalize_packaging_payload([
            {"label": "OK", "qty": 1, "unit": "palette"},
            {"label": "Empty", "qty": 0, "unit": "palette"},
            {"label": "Negative", "qty": -3, "unit": "palette"},
        ])
        assert len(result) == 1
        assert result[0]["label"] == "OK"

    def test_filters_empty_label(self):
        from common.services.loading_service import normalize_packaging_payload

        result = normalize_packaging_payload([
            {"label": "", "qty": 2, "unit": "palette"},
            {"label": "   ", "qty": 3, "unit": "palette"},
        ])
        assert result == []

    def test_default_unit_is_palette(self):
        from common.services.loading_service import normalize_packaging_payload

        result = normalize_packaging_payload([{"label": "X", "qty": 1}])
        assert result[0]["unit"] == "palette"

    def test_none_input(self):
        from common.services.loading_service import normalize_packaging_payload

        assert normalize_packaging_payload(None) == []
        assert normalize_packaging_payload([]) == []

    def test_skips_non_dict(self):
        from common.services.loading_service import normalize_packaging_payload

        result = normalize_packaging_payload([
            "not-a-dict",
            None,
            {"label": "OK", "qty": 1},
        ])
        assert len(result) == 1


# ─── send_previsionnel (orchestrateur métier) ───────────────────────────────

class TestSendPrevisionnel:
    """Tests de l'orchestration end-to-end de l'envoi prévisionnel.

    On mock TOUS les helpers externes (DB, PDF, email) — on vérifie
    uniquement que le service appelle les bons helpers avec les bons args,
    et propage les erreurs métier en ValueError.
    """

    _SOFRIPA_OBJ = {
        "name": "SOFRIPA",
        "address_lines": ["ZAC du Haut de Wissous II,", "91320 Wissous"],
        "email_recipients": ["exploitation@sofripa.fr", "z.dawam@sofripa.fr"],
        "packaging_items": [],
    }

    def _setup_happy_path_mocks(self, monkeypatch):
        """Configure tous les mocks pour le scénario success.

        Depuis le refacto 2026-05, send_previsionnel n'utilise plus
        link_palettes_to_ramasse ni rebuild_lines_from_palettes (workflow J1
        informatif). On mock lookup_sscc_batch + aggregate_palettes_to_lines.
        """
        monkeypatch.setattr(
            loading_service, "_resolve_destinataire",
            lambda name: self._SOFRIPA_OBJ if name == "SOFRIPA" else None,
        )
        # 2 palettes valides dans le lookup batch (mêmes SSCC que les tests
        # qui passent ["111111111111111111", "222222222222222222"]).
        fake_palettes = {
            "111111111111111111": loading_service.PaletteInfo(
                sscc="111111111111111111",
                gtin_palette="03760123456789",
                lot="L26045",
                ddm=_dt.date(2027, 5, 11),
                case_count=96,
                designation="Kéfir Pêche",
                fmt="12x33",
                marque="Symbiose",
                gout="Pêche",
                pcb=12,
                gtin_uvc="03760000000000",
                generated_at=_dt.datetime(2026, 5, 20, 10, 0),
            ),
            "222222222222222222": loading_service.PaletteInfo(
                sscc="222222222222222222",
                gtin_palette="03760123456789",
                lot="L26045",
                ddm=_dt.date(2027, 5, 11),
                case_count=96,
                designation="Kéfir Pêche",
                fmt="12x33",
                marque="Symbiose",
                gout="Pêche",
                pcb=12,
                gtin_uvc="03760000000000",
                generated_at=_dt.datetime(2026, 5, 20, 11, 0),
            ),
        }
        monkeypatch.setattr(
            loading_service, "lookup_sscc_batch",
            lambda sscc_list, tenant_id: {
                s: p for s, p in fake_palettes.items() if s in (sscc_list or [])
            },
        )
        monkeypatch.setattr(
            loading_service, "aggregate_palettes_to_lines",
            lambda palettes, carton_weight_fn=None: [
                {"ref": "X", "produit": "Kéfir 33cl", "ddm": "11/05/2027",
                 "cartons": 192, "palettes": 2, "poids": 1600},
            ] if palettes else [],
        )

    def test_unknown_destinataire_raises_value_error(self, monkeypatch):
        monkeypatch.setattr(
            loading_service, "_resolve_destinataire", lambda _name: None,
        )
        with pytest.raises(ValueError, match="Destinataire inconnu"):
            loading_service.send_previsionnel(
                "tenant-A",
                user_id="u1", user_email="op@sym.fr",
                destinataire="INCONNU",
                date_ramasse=_dt.date(2026, 5, 20),
                sscc_list=[],
            )

    def test_dest_without_emails_raises_value_error(self, monkeypatch):
        empty_dest = {**self._SOFRIPA_OBJ, "email_recipients": []}
        monkeypatch.setattr(
            loading_service, "_resolve_destinataire", lambda _: empty_dest,
        )
        with pytest.raises(ValueError, match="Aucun email configuré"):
            loading_service.send_previsionnel(
                "tenant-A",
                user_id="u1", user_email="",
                destinataire="SOFRIPA",
                date_ramasse=_dt.date(2026, 5, 20),
                sscc_list=[],
            )

    def test_active_ramasse_lock_propagates(self, monkeypatch):
        self._setup_happy_path_mocks(monkeypatch)
        with mock.patch(
            "common.ramasse_history.save_ramasse",
            side_effect=ValueError("Une ramasse est déjà en cours"),
        ):
            with pytest.raises(ValueError, match="déjà en cours"):
                loading_service.send_previsionnel(
                    "tenant-A",
                    user_id="u1", user_email="op@sym.fr",
                    destinataire="SOFRIPA",
                    date_ramasse=_dt.date(2026, 5, 20),
                    sscc_list=["111111111111111111"],
                )

    def test_happy_path_calls_all_helpers_in_order(self, monkeypatch):
        self._setup_happy_path_mocks(monkeypatch)
        with (
            mock.patch("common.ramasse_history.save_ramasse") as m_save,
            mock.patch("common.ramasse_history.finalize_ramasse_lines") as m_fin,
            mock.patch("common.xlsx_fill.bl_pdf.build_bl_enlevements_pdf") as m_pdf,
            mock.patch("common.email.send_html_with_pdf") as m_mail,
        ):
            m_save.return_value = "ramasse-uuid"
            m_pdf.return_value = b"%PDF-fake"
            m_mail.return_value = {"status": "sent"}

            result = loading_service.send_previsionnel(
                "tenant-A",
                user_id="u1", user_email="op@sym.fr",
                destinataire="SOFRIPA",
                date_ramasse=_dt.date(2026, 5, 20),
                sscc_list=["111111111111111111", "222222222222222222"],
                packaging=[
                    {"label": "Palette Bouteilles 33cl", "qty": 2, "unit": "palette"},
                ],
            )

        # save_ramasse appelé avec status="previsionnel" + tenant + user
        save_kwargs = m_save.call_args.kwargs
        assert save_kwargs["status"] == "previsionnel"
        assert save_kwargs["tenant_id"] == "tenant-A"
        assert save_kwargs["user_id"] == "u1"
        assert save_kwargs["destinataire"] == "SOFRIPA"
        # user_email ajouté aux recipients de SOFRIPA
        assert "op@sym.fr" in save_kwargs["recipients"]
        assert "exploitation@sofripa.fr" in save_kwargs["recipients"]
        # packaging normalisé (qty validée)
        assert save_kwargs["packaging"] == [
            {"label": "Palette Bouteilles 33cl", "qty": 2, "unit": "palette"},
        ]

        # PDF généré avec kind=previsionnel
        pdf_kwargs = m_pdf.call_args.kwargs
        assert pdf_kwargs["kind"] == "previsionnel"
        assert pdf_kwargs["destinataire_title"] == "SOFRIPA"

        # finalize_ramasse_lines patche les lignes/totaux/PDF
        fin_kwargs = m_fin.call_args.kwargs
        assert fin_kwargs["total_palettes"] == 2
        assert fin_kwargs["total_cartons"] == 192
        assert fin_kwargs["pdf_bytes"] == b"%PDF-fake"
        # Le snapshot SSCC est bien persisté pour le diff au J2 — sorted
        # pour matcher le tri appliqué par send_previsionnel (déterminisme).
        assert fin_kwargs["previsionnel_sscc_list"] == [
            "111111111111111111", "222222222222222222",
        ]

        # Mail envoyé aux recipients
        mail_kwargs = m_mail.call_args.kwargs
        assert "exploitation@sofripa.fr" in mail_kwargs["to_email"]
        assert "BL_Provisoire_20260520.pdf" in mail_kwargs["attachments"][0][0]

        # Réponse
        assert result["id"] == "ramasse-uuid"
        assert result["total_palettes"] == 2
        assert result["email_sent"] is True
        assert result["inserted"] == 2

    def test_does_not_link_palettes_at_j1(self, monkeypatch):
        """Garde-fou : depuis 2026-05, send_previsionnel ne doit PLUS lier
        les palettes au prévisionnel J1. palette_loadings reste vide
        jusqu'au scan J2. Si quelqu'un re-introduit l'appel par accident,
        ce test détecte la régression.
        """
        self._setup_happy_path_mocks(monkeypatch)
        with (
            mock.patch("common.ramasse_history.save_ramasse") as m_save,
            mock.patch("common.ramasse_history.finalize_ramasse_lines"),
            mock.patch(
                "common.xlsx_fill.bl_pdf.build_bl_enlevements_pdf",
                return_value=b"%PDF",
            ),
            mock.patch("common.email.send_html_with_pdf"),
            mock.patch.object(
                loading_service, "link_palettes_to_ramasse",
            ) as m_link,
        ):
            m_save.return_value = "ramasse-no-link"
            loading_service.send_previsionnel(
                "tenant-A",
                user_id="u1", user_email="op@sym.fr",
                destinataire="SOFRIPA",
                date_ramasse=_dt.date(2026, 5, 20),
                sscc_list=["111111111111111111", "222222222222222222"],
            )

        m_link.assert_not_called()

    def test_email_failure_returns_email_sent_false(self, monkeypatch):
        """Si send_html_with_pdf raise, la ramasse reste créée mais
        email_sent=False — l'opérateur sait qu'il doit réagir."""
        self._setup_happy_path_mocks(monkeypatch)
        with (
            mock.patch("common.ramasse_history.save_ramasse") as m_save,
            mock.patch("common.ramasse_history.finalize_ramasse_lines"),
            mock.patch(
                "common.xlsx_fill.bl_pdf.build_bl_enlevements_pdf",
                return_value=b"%PDF",
            ),
            mock.patch(
                "common.email.send_html_with_pdf",
                side_effect=RuntimeError("SMTP down"),
            ),
        ):
            m_save.return_value = "ramasse-1"
            result = loading_service.send_previsionnel(
                "tenant-A",
                user_id="u1", user_email="op@sym.fr",
                destinataire="SOFRIPA",
                date_ramasse=_dt.date(2026, 5, 20),
                sscc_list=["111111111111111111"],
            )
        assert result["email_sent"] is False
        assert result["id"] == "ramasse-1"


# ─── send_packaging_request (demande emballages sans ramasse) ───────────────

class TestSendPackagingRequest:
    """Tests de l'envoi de demande d'emballages séparée du formulaire ramasse.

    Le service est minimaliste : résout destinataire, génère email HTML
    (sans PDF), envoie via Brevo, trace audit. Aucune écriture DB métier.
    """

    _SOFRIPA_OBJ = {
        "name": "SOFRIPA",
        "address_lines": ["ZAC du Haut de Wissous II,", "91320 Wissous"],
        "email_recipients": ["exploitation@sofripa.fr", "prepa@sofripa.fr"],
        "packaging_items": [],
    }

    _ITEMS = [
        {"label": "Palette Bouteilles 33cl", "qty": 5, "unit": "palette"},
        {"label": "Palette Bouteilles 75cl", "qty": 3, "unit": "palette"},
    ]

    def _patch_email_ok(self, monkeypatch, capture):
        """Mock send_html_with_pdf qui capture les args et renvoie OK."""
        import common.email as email_mod

        def fake_send(**kwargs):
            capture.update(kwargs)
            return {"status": "sent", "provider_msg_id": "msg-1"}

        monkeypatch.setattr(email_mod, "send_html_with_pdf", fake_send)
        # Audit no-op (sinon tente d'écrire en DB)
        import common.audit
        monkeypatch.setattr(common.audit, "log_event", lambda **kw: None)

    def test_happy_path_sends_email_with_items(self, monkeypatch):
        monkeypatch.setattr(
            loading_service, "_resolve_destinataire",
            lambda name: self._SOFRIPA_OBJ if name == "SOFRIPA" else None,
        )
        captured = {}
        self._patch_email_ok(monkeypatch, captured)

        result = loading_service.send_packaging_request(
            "tenant-A",
            user_email="op@sym.fr",
            destinataire="SOFRIPA",
            date_ramasse=_dt.date(2026, 5, 25),
            items=self._ITEMS,
        )

        assert result["email_sent"] is True
        assert result["items_count"] == 2
        assert result["destinataire"] == "SOFRIPA"
        assert result["date_ramasse"] == "2026-05-25"
        # Email a bien été envoyé aux 2 recipients SOFRIPA
        assert captured["to_email"] == [
            "exploitation@sofripa.fr", "prepa@sofripa.fr",
        ]
        # Body contient les items
        assert "Palette Bouteilles 33cl" in captured["html_body"]
        assert "Palette Bouteilles 75cl" in captured["html_body"]
        # Date de livraison dans le sujet
        assert "25/05/2026" in captured["subject"]

    def test_unknown_destinataire_raises(self, monkeypatch):
        monkeypatch.setattr(
            loading_service, "_resolve_destinataire", lambda _name: None,
        )
        with pytest.raises(ValueError, match="Destinataire inconnu"):
            loading_service.send_packaging_request(
                "tenant-A",
                user_email="op@sym.fr",
                destinataire="INCONNU",
                date_ramasse=_dt.date(2026, 5, 25),
                items=self._ITEMS,
            )

    def test_no_email_configured_raises(self, monkeypatch):
        monkeypatch.setattr(
            loading_service, "_resolve_destinataire",
            lambda _name: {**self._SOFRIPA_OBJ, "email_recipients": []},
        )
        with pytest.raises(ValueError, match="Pas d'emails configurés"):
            loading_service.send_packaging_request(
                "tenant-A",
                user_email="op@sym.fr",
                destinataire="SOFRIPA",
                date_ramasse=_dt.date(2026, 5, 25),
                items=self._ITEMS,
            )

    def test_empty_items_raises(self, monkeypatch):
        monkeypatch.setattr(
            loading_service, "_resolve_destinataire",
            lambda _name: self._SOFRIPA_OBJ,
        )
        with pytest.raises(ValueError, match="Aucun emballage"):
            loading_service.send_packaging_request(
                "tenant-A",
                user_email="op@sym.fr",
                destinataire="SOFRIPA",
                date_ramasse=_dt.date(2026, 5, 25),
                items=[],
            )

    def test_items_with_zero_qty_filtered_out(self, monkeypatch):
        # qty=0 → normalize_packaging_payload les filtre → "Aucun emballage"
        monkeypatch.setattr(
            loading_service, "_resolve_destinataire",
            lambda _name: self._SOFRIPA_OBJ,
        )
        with pytest.raises(ValueError, match="Aucun emballage"):
            loading_service.send_packaging_request(
                "tenant-A",
                user_email="op@sym.fr",
                destinataire="SOFRIPA",
                date_ramasse=_dt.date(2026, 5, 25),
                items=[{"label": "Palette", "qty": 0, "unit": "palette"}],
            )

    def test_email_send_failure_returns_email_sent_false(self, monkeypatch):
        # L'email plante → le service ne lève pas, retourne email_sent=False
        # (best-effort, l'audit log enregistre quand même)
        monkeypatch.setattr(
            loading_service, "_resolve_destinataire",
            lambda _name: self._SOFRIPA_OBJ,
        )
        import common.email as email_mod

        def fake_send_fail(**kwargs):
            raise RuntimeError("Brevo down")

        monkeypatch.setattr(email_mod, "send_html_with_pdf", fake_send_fail)
        import common.audit
        monkeypatch.setattr(common.audit, "log_event", lambda **kw: None)

        result = loading_service.send_packaging_request(
            "tenant-A",
            user_email="op@sym.fr",
            destinataire="SOFRIPA",
            date_ramasse=_dt.date(2026, 5, 25),
            items=self._ITEMS,
        )
        assert result["email_sent"] is False
        assert result["items_count"] == 2


# ─── finalize_loading (orchestrateur métier) ────────────────────────────────

class TestFinalizeLoading:
    """Tests de la transition previsionnel → definitif + PDF + email."""

    _SOFRIPA_OBJ = {
        "name": "SOFRIPA",
        "address_lines": ["ZAC ..."],
        "email_recipients": ["exploitation@sofripa.fr"],
    }

    def test_ramasse_not_found_raises_value_error(self):
        with mock.patch(
            "common.ramasse_history.get_ramasse", return_value=None,
        ):
            with pytest.raises(ValueError, match="introuvable"):
                loading_service.finalize_loading(
                    "tenant-A", ramasse_id="missing", user_email="op@sym.fr",
                )

    def test_non_previsionnel_status_raises_value_error(self):
        ramasse = {
            "id": "r1", "status": "definitif",  # déjà finalisée
            "destinataire": "SOFRIPA", "date_ramasse": _dt.date(2026, 5, 20),
            "lines": [], "packaging": [], "version": 2,
        }
        with mock.patch(
            "common.ramasse_history.get_ramasse", return_value=ramasse,
        ):
            with pytest.raises(ValueError, match="previsionnel"):
                loading_service.finalize_loading(
                    "tenant-A", ramasse_id="r1", user_email="op@sym.fr",
                )

    def test_happy_path_returns_info_and_pdf(self, monkeypatch):
        monkeypatch.setattr(
            loading_service, "_resolve_destinataire",
            lambda _name: self._SOFRIPA_OBJ,
        )
        monkeypatch.setattr(
            loading_service, "rebuild_lines_from_palettes",
            lambda rid, tid, **kw: (
                [{"ref": "X", "produit": "Kéfir", "ddm": "11/05/2027",
                  "cartons": 480, "palettes": 5, "poids": 4000}],
                480, 5, 4000,
            ),
        )

        ramasse = {
            "id": "r1", "status": "previsionnel",
            "destinataire": "SOFRIPA",
            "date_ramasse": _dt.date(2026, 5, 20),
            "lines": [
                {"ref": "X", "produit": "Kéfir", "cartons": 384, "palettes": 4}
            ],
            "packaging": [{"label": "Palette 33cl", "qty": 2, "unit": "palette"}],
            "recipients": ["exploitation@sofripa.fr"],
            "version": 1,
        }

        with (
            mock.patch(
                "common.ramasse_history.get_ramasse", return_value=ramasse,
            ),
            mock.patch(
                "common.ramasse_history.update_ramasse",
                return_value={"id": "r1", "version": 2},
            ) as m_update,
            mock.patch(
                "common.xlsx_fill.bl_pdf.build_bl_enlevements_pdf",
                return_value=b"%PDF-final",
            ) as m_pdf,
            mock.patch(
                "common.email.send_html_with_pdf",
                return_value={"status": "sent"},
            ),
        ):
            info, pdf = loading_service.finalize_loading(
                "tenant-A", ramasse_id="r1", user_email="op@sym.fr",
            )

        # Info de retour
        assert info["id"] == "r1"
        assert info["total_palettes"] == 5
        assert info["total_cartons"] == 480
        assert info["email_sent"] is True
        assert info["version"] == 2
        assert "op@sym.fr" in info["recipients"]
        # PDF binaire renvoyé
        assert pdf == b"%PDF-final"

        # PDF généré avec kind=definitif + previous_lines (diff)
        pdf_kwargs = m_pdf.call_args.kwargs
        assert pdf_kwargs["kind"] == "definitif"
        assert pdf_kwargs["version"] == 2
        assert pdf_kwargs["previous_lines"] == [
            {"ref": "X", "produit": "Kéfir", "cartons": 384, "palettes": 4},
        ]

        # update_ramasse transitionne au statut definitif
        upd_kwargs = m_update.call_args.kwargs
        assert upd_kwargs["target_status"] == "definitif"
        assert upd_kwargs["tenant_id"] == "tenant-A"

    def test_update_refused_raises_value_error(self, monkeypatch):
        """Cas où update_ramasse renvoie None (transition refusée ou
        chauffeur déjà passé) → ValueError."""
        monkeypatch.setattr(
            loading_service, "_resolve_destinataire",
            lambda _: self._SOFRIPA_OBJ,
        )
        monkeypatch.setattr(
            loading_service, "rebuild_lines_from_palettes",
            lambda rid, tid, **kw: ([], 0, 0, 0),
        )
        ramasse = {
            "id": "r1", "status": "previsionnel",
            "destinataire": "SOFRIPA",
            "date_ramasse": _dt.date(2026, 5, 20),
            "lines": [], "packaging": [],
            "recipients": [], "version": 1,
        }
        with (
            mock.patch(
                "common.ramasse_history.get_ramasse", return_value=ramasse,
            ),
            mock.patch(
                "common.ramasse_history.update_ramasse", return_value=None,
            ),
            mock.patch(
                "common.xlsx_fill.bl_pdf.build_bl_enlevements_pdf",
                return_value=b"%PDF",
            ),
        ):
            with pytest.raises(ValueError, match="Transition refusée"):
                loading_service.finalize_loading(
                    "tenant-A", ramasse_id="r1", user_email="op@sym.fr",
                )

    def test_email_failure_still_returns_pdf(self, monkeypatch):
        """Si SMTP plante, on récupère quand même le PDF + email_sent=False."""
        monkeypatch.setattr(
            loading_service, "_resolve_destinataire",
            lambda _: self._SOFRIPA_OBJ,
        )
        monkeypatch.setattr(
            loading_service, "rebuild_lines_from_palettes",
            lambda rid, tid, **kw: ([], 0, 0, 0),
        )
        ramasse = {
            "id": "r1", "status": "previsionnel",
            "destinataire": "SOFRIPA",
            "date_ramasse": _dt.date(2026, 5, 20),
            "lines": [], "packaging": [],
            "recipients": [], "version": 1,
        }
        with (
            mock.patch(
                "common.ramasse_history.get_ramasse", return_value=ramasse,
            ),
            mock.patch(
                "common.ramasse_history.update_ramasse",
                return_value={"id": "r1"},
            ),
            mock.patch(
                "common.xlsx_fill.bl_pdf.build_bl_enlevements_pdf",
                return_value=b"%PDF",
            ),
            mock.patch(
                "common.email.send_html_with_pdf",
                side_effect=RuntimeError("SMTP down"),
            ),
        ):
            info, pdf = loading_service.finalize_loading(
                "tenant-A", ramasse_id="r1", user_email="op@sym.fr",
            )
        assert info["email_sent"] is False
        assert pdf == b"%PDF"
