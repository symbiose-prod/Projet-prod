"""Tests for common/services/etiquette_palette_service — pure business logic.

Le service contient deux types de fonctions :
  - logique pure (compute_case_count, build_gs1_128_payload, compute_ddm_*)
    → testée ici sans mock
  - I/O EasyBeer (load_initial_data) → testée avec monkeypatch
"""
from __future__ import annotations

import datetime as _dt

import pytest

from common.ramasse import get_palette_layout
from common.services.etiquette_palette_service import (
    Gs1Payload,
    ProductFormat,
    build_gs1_128_payload,
    compute_case_count,
    compute_ddm_from_brassin_code,
    load_initial_data,
)

# ─── compute_case_count ──────────────────────────────────────────────────────

class TestComputeCaseCount:

    def test_full_pallet_12x33(self):
        assert compute_case_count("12x33", full_pallet=True) == 126

    def test_full_pallet_6x75_niko_override(self):
        """Niko keyword applies override : 4×21 = 84 instead of 4×24 = 96."""
        assert compute_case_count("6x75", full_pallet=True, product_label="Kéfir Niko") == 84

    def test_partial_pallet_basic(self):
        """3 étages × 18 + 5 caisses = 59."""
        result = compute_case_count(
            "12x33", full_pallet=False, layers_full=3, extras_top=5,
        )
        assert result == 59

    def test_partial_zero(self):
        result = compute_case_count("12x33", full_pallet=False, layers_full=0, extras_top=0)
        assert result == 0

    def test_partial_max_layers_no_extras(self):
        """7 étages pleins = palette pleine (mais via mode partial)."""
        result = compute_case_count("12x33", full_pallet=False, layers_full=7, extras_top=0)
        assert result == 126

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError, match="Format de palette inconnu"):
            compute_case_count("99x99", full_pallet=True)

    def test_layers_too_high_raises(self):
        with pytest.raises(ValueError, match="layers_full"):
            compute_case_count("12x33", full_pallet=False, layers_full=8, extras_top=0)

    def test_extras_equal_per_layer_raises(self):
        """extras_top doit être < per_layer (un étage complet → incrémenter layers_full)."""
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
            lot="KME27042026",
            ddm=_dt.date(2026, 9, 1),
            count=126,
        )
        defaults.update(overrides)
        return build_gs1_128_payload(**defaults)

    def test_content_structure(self):
        p = self._payload()
        # AI 01 (14 digits) + AI 15 (6 digits) + AI 37 (3 digits) + AI 10 (variable)
        assert p.content == "0103770014427014" + "15260901" + "37126" + "10KME27042026"

    def test_hri_has_parens(self):
        p = self._payload()
        assert "(01)" in p.hri
        assert "(15)" in p.hri
        assert "(37)" in p.hri
        assert "(10)" in p.hri

    def test_ean13_to_gtin14_padding(self):
        """EAN-13 → GTIN-14 par préfixage avec '0'."""
        p = self._payload(ean13="3770014427014")
        assert p.content.startswith("01" + "0" + "3770014427014")

    def test_gtin14_passthrough(self):
        """Si on passe déjà 14 digits, pas de padding supplémentaire."""
        p = self._payload(ean13="03770014427014")
        assert p.content.startswith("01" + "03770014427014")

    def test_invalid_ean_raises(self):
        with pytest.raises(ValueError, match="EAN/GTIN invalide"):
            self._payload(ean13="123")

    def test_count_padding(self):
        """count est padding sur 3 digits."""
        assert self._payload(count=5).content.endswith("3700510KME27042026")

    def test_count_max(self):
        assert self._payload(count=999).content[:].count("37999") == 1

    def test_count_too_high_raises(self):
        with pytest.raises(ValueError, match="count"):
            self._payload(count=1000)

    def test_count_zero_raises(self):
        with pytest.raises(ValueError, match="count"):
            self._payload(count=0)

    def test_lot_normalization_uppercase(self):
        p = self._payload(lot="kme27042026")
        assert "10KME27042026" in p.content

    def test_lot_strips_invalid_chars(self):
        """Les caractères non-GS1 sont retirés (accents, espaces, etc.)."""
        p = self._payload(lot="KMÉ 27/04 2026")
        # 'É' retiré, espaces retirés, '/' conservé
        assert "10KM27/042026" in p.content

    def test_lot_truncated_at_20(self):
        long_lot = "A" * 30
        p = self._payload(lot=long_lot)
        # Lot tronqué à 20 chars max (contrainte AI 10)
        assert p.content.endswith("10" + "A" * 20)

    def test_lot_empty_raises(self):
        with pytest.raises(ValueError, match="Lot vide"):
            self._payload(lot="!!!")

    def test_ddm_format_yymmdd(self):
        """AI 15 = YYMMDD (2 digits année)."""
        p = self._payload(ddm=_dt.date(2027, 4, 27))
        assert "15270427" in p.content


# ─── compute_ddm_from_brassin_code ──────────────────────────────────────────

class TestComputeDdmFromBrassinCode:

    def test_kefir_code_pattern(self):
        # 'KME27042026' → date métier 27/04/2026 + 365 jours
        ddm = compute_ddm_from_brassin_code("KME27042026")
        assert ddm == _dt.date(2027, 4, 27)

    def test_infusion_code_pattern(self):
        # 'IPM01092025' → 01/09/2025 + 365j = 01/09/2026
        ddm = compute_ddm_from_brassin_code("IPM01092025")
        assert ddm == _dt.date(2026, 9, 1)

    def test_invalid_pattern_returns_none(self):
        assert compute_ddm_from_brassin_code("garbage") is None
        assert compute_ddm_from_brassin_code("") is None
        assert compute_ddm_from_brassin_code(None) is None


# ─── load_initial_data (avec monkeypatch des fetchers EasyBeer) ─────────────

class TestLoadInitialData:

    def test_happy_path(self, monkeypatch):
        # Mock matrice codes-barres (format EasyBeer brut)
        monkeypatch.setattr(
            "common.services.etiquette_palette_service.get_code_barre_matrice",
            lambda: {
                "produits": [
                    {
                        "codesBarres": [
                            {
                                "code": "3770014427014",
                                "modeleProduit": {"idProduit": 42},
                                "modeleContenant": {"contenance": 0.33},
                                "modeleLot": {"libelle": "Carton de 12"},
                            },
                        ],
                    },
                ],
            },
        )
        # Mock liste produits — le suffixe degré "- 0.0°" doit être nettoyé
        monkeypatch.setattr(
            "common.services.etiquette_palette_service.get_all_products",
            lambda: [{"idProduit": 42, "libelle": "Kéfir Mangue Passion - 0.0°"}],
        )
        # Mock brassins en cours
        monkeypatch.setattr(
            "common.services.etiquette_palette_service.get_brassins_en_cours_cached",
            lambda: [{
                "idBrassin": 999,
                "nom": "KME27042026",
                "produit": {"libelle": "Kéfir Mangue Passion - 0.0°"},
            }],
        )

        data = load_initial_data()
        assert data.errors == []
        assert len(data.products) == 1
        pf = data.products[0]
        assert pf.id_produit == 42
        assert pf.fmt == "12x33"
        assert pf.ean13 == "3770014427014"
        assert pf.libelle == "Kéfir Mangue Passion"  # nettoyé du suffixe "- 0.0°"

        assert len(data.brassins) == 1
        b = data.brassins[0]
        assert b.id_brassin == 999
        assert b.code == "KME27042026"
        assert b.ddm_date == _dt.date(2027, 4, 27)

    def test_partial_failure_brassins(self, monkeypatch):
        """Si get_brassins_en_cours_cached échoue, on garde les produits."""
        from common.easybeer import EasyBeerError

        monkeypatch.setattr(
            "common.services.etiquette_palette_service.get_code_barre_matrice",
            lambda: {"produits": []},
        )
        monkeypatch.setattr(
            "common.services.etiquette_palette_service.get_all_products",
            lambda: [],
        )

        def _boom():
            raise EasyBeerError("fake transport error")

        monkeypatch.setattr(
            "common.services.etiquette_palette_service.get_brassins_en_cours_cached",
            _boom,
        )

        data = load_initial_data()
        assert data.products == []
        assert data.brassins == []
        assert any("Brassins indisponibles" in e for e in data.errors)


# ─── Sanity checks sur les dataclasses ───────────────────────────────────────

class TestDataclasses:

    def test_product_format_immutable(self):
        pf = ProductFormat(
            id_produit=1, libelle="X", fmt="12x33", ean13="0" * 13, lot_label="",
        )
        with pytest.raises(Exception):
            pf.fmt = "6x33"  # type: ignore[misc]

    def test_palette_layout_consistency(self):
        """Sanity : les layouts en config doivent donner les bons totaux."""
        assert get_palette_layout("12x33")["total"] == 126
        assert get_palette_layout("6x33")["total"] == 252
        assert get_palette_layout("6x75")["total"] == 96
        assert get_palette_layout("6x75", "Kéfir Niko")["total"] == 84
        assert get_palette_layout("4x75")["total"] == 112
