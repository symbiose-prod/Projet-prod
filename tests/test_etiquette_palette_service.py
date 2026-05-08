"""Tests for common/services/etiquette_palette_service — pure business logic.

Le service contient deux types de fonctions :
  - logique pure (compute_case_count, build_gs1_128_payload, classify_*, ...)
    → testée ici sans mock
  - I/O DB (load_label_data_from_sync) → testée avec monkeypatch sur run_sql
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from common.ramasse import get_palette_layout
from common.services.etiquette_palette_service import (
    BOTTLE_33,
    BOTTLE_75_EAU_GAZ,
    BOTTLE_75_SAFT,
    BRAND_NIKO,
    BRAND_SYMBIOSE,
    Gs1Payload,
    LabelEntry,
    _format_lot_str,
    _parse_ddm_iso,
    build_gs1_128_payload,
    classify_bottle_type,
    compute_case_count,
    extract_label_gout,
    load_label_data_from_sync,
)

# ─── compute_case_count ──────────────────────────────────────────────────────

class TestComputeCaseCount:

    def test_full_pallet_12x33(self):
        assert compute_case_count("12x33", full_pallet=True) == 126

    def test_full_pallet_6x75_niko_override(self):
        assert compute_case_count("6x75", full_pallet=True, product_label="Kéfir Niko") == 84

    def test_partial_pallet_basic(self):
        result = compute_case_count(
            "12x33", full_pallet=False, layers_full=3, extras_top=5,
        )
        assert result == 59

    def test_partial_zero(self):
        result = compute_case_count("12x33", full_pallet=False, layers_full=0, extras_top=0)
        assert result == 0

    def test_partial_max_layers_no_extras(self):
        result = compute_case_count("12x33", full_pallet=False, layers_full=7, extras_top=0)
        assert result == 126

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError, match="Format de palette inconnu"):
            compute_case_count("99x99", full_pallet=True)

    def test_layers_too_high_raises(self):
        with pytest.raises(ValueError, match="layers_full"):
            compute_case_count("12x33", full_pallet=False, layers_full=8, extras_top=0)

    def test_extras_equal_per_layer_raises(self):
        with pytest.raises(ValueError, match="extras_top"):
            compute_case_count("12x33", full_pallet=False, layers_full=2, extras_top=18)

    def test_extras_negative_raises(self):
        with pytest.raises(ValueError, match="extras_top"):
            compute_case_count("12x33", full_pallet=False, layers_full=2, extras_top=-1)


# ─── build_gs1_128_payload ───────────────────────────────────────────────────

class TestBuildGs1128Payload:

    def _payload(self, **overrides) -> Gs1Payload:
        defaults = dict(
            ean13="3770014427014",
            lot="08052027",
            ddm=_dt.date(2026, 9, 1),
            count=126,
        )
        defaults.update(overrides)
        return build_gs1_128_payload(**defaults)

    def test_content_structure(self):
        p = self._payload()
        assert p.content == "0103770014427014" + "15260901" + "37126" + "1008052027"

    def test_hri_has_parens(self):
        p = self._payload()
        assert "(01)" in p.hri
        assert "(15)" in p.hri
        assert "(37)" in p.hri
        assert "(10)" in p.hri

    def test_ean13_to_gtin14_padding(self):
        p = self._payload(ean13="3770014427014")
        assert p.content.startswith("01" + "0" + "3770014427014")

    def test_gtin14_passthrough(self):
        p = self._payload(ean13="03770014427014")
        assert p.content.startswith("01" + "03770014427014")

    def test_invalid_ean_raises(self):
        with pytest.raises(ValueError, match="EAN/GTIN invalide"):
            self._payload(ean13="123")

    def test_count_padding(self):
        assert self._payload(count=5).content.endswith("370051008052027")

    def test_count_too_high_raises(self):
        with pytest.raises(ValueError, match="count"):
            self._payload(count=1000)

    def test_count_zero_raises(self):
        with pytest.raises(ValueError, match="count"):
            self._payload(count=0)

    def test_lot_normalization_uppercase(self):
        p = self._payload(lot="km27042026")
        assert "10KM27042026" in p.content

    def test_lot_strips_invalid_chars(self):
        p = self._payload(lot="KMÉ 27/04 2026")
        assert "10KM27/042026" in p.content

    def test_lot_truncated_at_20(self):
        long_lot = "A" * 30
        p = self._payload(lot=long_lot)
        assert p.content.endswith("10" + "A" * 20)

    def test_lot_empty_raises(self):
        with pytest.raises(ValueError, match="Lot vide"):
            self._payload(lot="!!!")

    def test_ddm_format_yymmdd(self):
        p = self._payload(ddm=_dt.date(2027, 4, 27))
        assert "15270427" in p.content


# ─── classify_bottle_type ────────────────────────────────────────────────────

class TestClassifyBottleType:

    def test_33cl_symbiose(self):
        assert classify_bottle_type("Kéfir Gingembre — 12x33cl", "SYMBIOSE", 12) == BOTTLE_33
        assert classify_bottle_type("Kéfir Gingembre — 6x33cl", "SYMBIOSE", 6) == BOTTLE_33

    def test_33cl_niko(self):
        assert classify_bottle_type("NIKO - Kéfir Gingembre — 12x33cl", "NIKO", 12) == BOTTLE_33

    def test_75cl_saft_symbiose_4x(self):
        """Symbiose 4×75 = nouvelle bouteille SAFT."""
        assert classify_bottle_type("Kéfir Original — 4x75cl", "SYMBIOSE", 4) == BOTTLE_75_SAFT

    def test_75cl_eau_gazeuse_symbiose_6x(self):
        """Symbiose 6×75 = ancienne bouteille Verallia (eau gazeuse)."""
        assert classify_bottle_type("Kéfir Original — 6x75cl", "SYMBIOSE", 6) == BOTTLE_75_EAU_GAZ

    def test_75cl_niko_always_saft(self):
        """NIKO 75cl est toujours SAFT, peu importe le PCB."""
        assert classify_bottle_type("NIKO - Kéfir Gingembre — 6x75cl", "NIKO", 6) == BOTTLE_75_SAFT

    def test_unknown_format_returns_none(self):
        assert classify_bottle_type("Kéfir 50cl", "SYMBIOSE", 6) is None


# ─── extract_label_gout ──────────────────────────────────────────────────────

class TestExtractLabelGout:

    def test_kefir_simple(self):
        assert extract_label_gout("Kéfir Gingembre — 12x33cl", "SYMBIOSE") == "Gingembre"

    def test_kefir_de_fruits(self):
        assert extract_label_gout(
            "Kéfir de fruits Mangue Passion — 6x33cl", "SYMBIOSE",
        ) == "Mangue Passion"

    def test_niko_prefix_stripped(self):
        """Le préfixe 'NIKO - ' doit être retiré avant extraction du goût."""
        assert extract_label_gout(
            "NIKO - Kéfir de fruits Gingembre — 12x33cl", "NIKO",
        ) == "Gingembre"

    def test_infusion(self):
        assert extract_label_gout(
            "Infusion probiotique Zest d'agrumes — 6x33cl", "SYMBIOSE",
        ) == "Zest d'agrumes"


# ─── _format_lot_str ─────────────────────────────────────────────────────────

class TestFormatLotStr:

    def test_int(self):
        assert _format_lot_str(11032027) == "11032027"

    def test_float(self):
        assert _format_lot_str(11032027.0) == "11032027"

    def test_seven_digits_padded_to_eight(self):
        """8 mai → '08052027' (pas '8052027')."""
        assert _format_lot_str(8052027) == "08052027"

    def test_none(self):
        assert _format_lot_str(None) == ""

    def test_empty_string(self):
        assert _format_lot_str("") == ""


# ─── _parse_ddm_iso ──────────────────────────────────────────────────────────

class TestParseDdmIso:

    def test_iso_date(self):
        assert _parse_ddm_iso("2027-05-08") == _dt.date(2027, 5, 8)

    def test_iso_datetime_truncated(self):
        assert _parse_ddm_iso("2027-05-08T00:00:00.000Z") == _dt.date(2027, 5, 8)

    def test_invalid_returns_none(self):
        assert _parse_ddm_iso(None) is None
        assert _parse_ddm_iso("garbage") is None
        assert _parse_ddm_iso("") is None


# ─── load_label_data_from_sync ──────────────────────────────────────────────

class TestLoadLabelDataFromSync:

    @staticmethod
    def _sample_payload() -> list[dict]:
        return [
            {
                "designation": "Kéfir Gingembre — 6x33cl",
                "marque": "SYMBIOSE",
                "code_interne": "SK-KDF-33-GIN",
                "pcb": 6.0,
                "gtin_uvc": "3770014427014",
                "gtin_colis": "23770014427018",
                "lot": 8052027.0,
                "ddm": "2027-05-08",
            },
            {
                "designation": "Kéfir Gingembre — 4x75cl",
                "marque": "SYMBIOSE",
                "code_interne": "SK-KDF-75-GIN",
                "pcb": 4.0,
                "gtin_uvc": "3770014427045",
                "gtin_colis": "23770014427049",
                "lot": 8052027.0,
                "ddm": "2027-05-08",
            },
            {
                "designation": "NIKO - Kéfir de fruits Gingembre — 12x33cl",
                "marque": "NIKO",
                "code_interne": "NIKO-KDF-33-GIN",
                "pcb": 12.0,
                "gtin_uvc": "3770014427328",
                "gtin_colis": "13770014427325",
                "lot": 8052027.0,
                "ddm": "2027-05-08",
            },
        ]

    def test_happy_path(self, monkeypatch):
        sample = self._sample_payload()

        def _fake_run_sql(query, params=None):
            return [{
                "id": 42,
                "payload": sample,
                "status": "applied",
                "applied_at": _dt.datetime.now(),
                "created_at": _dt.datetime.now(),
            }]

        monkeypatch.setattr(
            "common.services.etiquette_palette_service.run_sql", _fake_run_sql,
        )

        entries, msg = load_label_data_from_sync("tenant-x")

        assert msg is None
        assert len(entries) == 3

        # Check Symbiose 33cl Gingembre
        e = next(x for x in entries if x.marque == "SYMBIOSE" and x.bottle_type == BOTTLE_33)
        assert e.gout == "Gingembre"
        assert e.fmt == "6x33"
        assert e.pcb == 6
        assert e.ean_colis == "23770014427018"
        assert e.lot_str == "08052027"
        assert e.ddm_date == _dt.date(2027, 5, 8)
        assert e.code_interne == "SK-KDF-33-GIN"

        # Check Symbiose 75cl SAFT
        e = next(x for x in entries if x.bottle_type == BOTTLE_75_SAFT and x.marque == "SYMBIOSE")
        assert e.gout == "Gingembre"
        assert e.pcb == 4

        # Check NIKO 33cl
        e = next(x for x in entries if x.marque == "NIKO")
        assert e.gout == "Gingembre"
        assert e.bottle_type == BOTTLE_33

    def test_payload_as_json_string(self, monkeypatch):
        """Postgres peut renvoyer le JSONB comme string selon le driver."""
        sample = self._sample_payload()
        monkeypatch.setattr(
            "common.services.etiquette_palette_service.run_sql",
            lambda *a, **kw: [{
                "id": 42, "payload": json.dumps(sample),
                "status": "applied", "applied_at": None, "created_at": None,
            }],
        )
        entries, msg = load_label_data_from_sync("tenant-x")
        assert msg is None
        assert len(entries) == 3

    def test_no_sync_returns_info(self, monkeypatch):
        monkeypatch.setattr(
            "common.services.etiquette_palette_service.run_sql", lambda *a, **kw: [],
        )
        entries, msg = load_label_data_from_sync("tenant-x")
        assert entries == []
        assert msg is not None
        assert "sync" in msg.lower()

    def test_pending_sync_warns(self, monkeypatch):
        sample = self._sample_payload()
        monkeypatch.setattr(
            "common.services.etiquette_palette_service.run_sql",
            lambda *a, **kw: [{
                "id": 42, "payload": sample,
                "status": "pending", "applied_at": None, "created_at": _dt.datetime.now(),
            }],
        )
        entries, msg = load_label_data_from_sync("tenant-x")
        assert len(entries) == 3
        assert msg is not None  # warning sur sync non appliquée

    def test_skips_entries_without_required_fields(self, monkeypatch):
        sample = [
            {
                "designation": "Kéfir Gingembre — 6x33cl",
                "marque": "SYMBIOSE", "pcb": 6,
                "gtin_colis": "23770014427018", "lot": 1, "ddm": "2027-05-08",
            },
            {
                "designation": "Pas d'EAN",
                "marque": "SYMBIOSE", "pcb": 6,
                "gtin_colis": "", "lot": 1, "ddm": "2027-05-08",
            },
            {
                "designation": "Pas de format",
                "marque": "SYMBIOSE", "pcb": 0,
                "gtin_colis": "1234", "lot": 1, "ddm": "2027-05-08",
            },
        ]
        monkeypatch.setattr(
            "common.services.etiquette_palette_service.run_sql",
            lambda *a, **kw: [{
                "id": 1, "payload": sample, "status": "applied",
                "applied_at": None, "created_at": None,
            }],
        )
        entries, _ = load_label_data_from_sync("tenant-x")
        # Seul le premier doit être conservé
        assert len(entries) == 1


# ─── Sanity checks ───────────────────────────────────────────────────────────

class TestDataclasses:

    def test_label_entry_immutable(self):
        e = LabelEntry(
            marque=BRAND_SYMBIOSE,
            bottle_type=BOTTLE_33,
            gout="Gingembre",
            designation="Kéfir Gingembre — 12x33cl",
            fmt="12x33",
            pcb=12,
            ean_colis="3770014427014",
            code_interne="SK-KDF-33-GIN",
            lot_str="08052027",
            ddm_date=_dt.date(2027, 5, 8),
            product_label="Kéfir Gingembre",
        )
        with pytest.raises(Exception):
            e.gout = "Mangue"  # type: ignore[misc]

    def test_palette_layout_consistency(self):
        assert get_palette_layout("12x33")["total"] == 126
        assert get_palette_layout("6x33")["total"] == 252
        assert get_palette_layout("6x75")["total"] == 96
        assert get_palette_layout("6x75", "Kéfir Niko")["total"] == 84
        assert get_palette_layout("4x75")["total"] == 112

    def test_brand_constants(self):
        assert BRAND_NIKO == "NIKO"
        assert BRAND_SYMBIOSE == "SYMBIOSE"
