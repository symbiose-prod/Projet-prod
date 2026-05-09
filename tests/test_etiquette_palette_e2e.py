"""Tests E2E : génération GS1-128 → décodage zxing-cpp → PDF.

On ne teste pas l'endpoint HTTP (qui nécessite un client NiceGUI complet),
mais le pipeline complet côté serveur :

    treepoem (générer image) → zxing-cpp (décoder) → service (enrichir) → PDF

Si tout tourne bout-en-bout, le décodage prod fonctionne aussi.
"""
from __future__ import annotations

import datetime as _dt
import io
from unittest.mock import patch

import pypdf

from common.etiquette_palette_pdf import EtiquetteContext, build_etiquette_palette_pdf
from common.services.etiquette_palette_service import (
    classify_bottle_type,
    extract_ean_from_image,
    extract_gs1_data_from_image,
    list_recent_labels,
    save_label_history,
)


def _generate_gs1_image(data_with_parens: str) -> bytes:
    """Génère un PNG contenant un GS1-128 via treepoem."""
    import treepoem
    img = treepoem.generate_barcode(barcode_type="gs1-128", data=data_with_parens)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# ─── Pipeline scan : image → décodage ────────────────────────────────────────

class TestScanPipeline:

    def test_decode_full_gs1_carton(self):
        """GS1 typique étiquette carton : (01)<GTIN>(15)<DLUO>(10)<lot>."""
        png = _generate_gs1_image("(01)03770014427250(15)270511(10)110527")
        result = extract_gs1_data_from_image(png)
        assert result is not None
        assert result["ean"] == "03770014427250"
        assert result["lot"] == "110527"
        assert result["ddm"] == _dt.date(2027, 5, 11)

    def test_decode_extract_ean_only(self):
        """``extract_ean_from_image`` retourne juste le GTIN."""
        png = _generate_gs1_image("(01)03770014427250(15)270511(10)110527")
        ean = extract_ean_from_image(png)
        assert ean == "03770014427250"

    def test_decode_palette_label_with_ai_02_returns_none(self):
        """Une étiquette palette (AI 02 + 37) n'est pas décodée : on n'extrait
        que les codes-barres carton (AI 01) — on ne re-scanne pas nos propres
        étiquettes palette, c'est le comportement souhaité."""
        png = _generate_gs1_image(
            "(02)03770014427250(15)270511(10)L1234(37)96",
        )
        result = extract_gs1_data_from_image(png)
        assert result is None


# ─── Pipeline complet : scan → PDF ──────────────────────────────────────────

class TestScanToPdf:

    def test_scan_then_generate_pdf_two_copies(self):
        """Pipeline complet : scan une étiquette carton, génère le PDF palette."""
        # 1. Le carton physique a un GS1-128 carton
        png = _generate_gs1_image("(01)03770014427250(15)270511(10)110527")
        scan = extract_gs1_data_from_image(png)
        assert scan is not None

        # 2. Avec ces données + un format/quantité saisis manuellement, on
        #    construit le contexte et on génère le PDF
        ctx = EtiquetteContext(
            product_label="Kéfir Pamplemousse Rose",
            fmt="6x75",
            ean13=scan["ean"],
            lot=scan["lot"],
            ddm=scan["ddm"],
            case_count=96,
            full_pallet=True,
            tenant_name="Symbiose Kéfir",
            n_copies=2,
        )
        pdf_bytes = build_etiquette_palette_pdf(ctx)

        # 3. Vérifie que le PDF est bien formé et contient 2 pages
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) == 2

        # 4. Vérifie que le contenu textuel contient les données scannées
        page0_text = reader.pages[0].extract_text()
        assert "PAMPLEMOUSSE" in page0_text.upper()
        assert "110527" in page0_text  # lot
        assert "11/05/2027" in page0_text  # DDM formatée jj/mm/aaaa
        assert "96" in page0_text  # quantité
        assert "FERMENT STATION" in page0_text
        assert "GTIN COLIS" in page0_text


# ─── lookup_product_by_ean (avec mocks EasyBeer) ────────────────────────────

class TestLookupProductByEan:

    @staticmethod
    def _mock_matrice():
        """Matrice CB EasyBeer minimale : 1 produit, format 6x33."""
        return {
            "produits": [
                {
                    "codesBarres": [
                        {
                            "code": "23770014427018",
                            "modeleProduit": {"idProduit": 42},
                            "modeleContenant": {"contenance": 0.33},
                            "modeleLot": {"libelle": "Carton de 6"},
                        },
                    ],
                },
            ],
        }

    @staticmethod
    def _mock_products():
        return [{"idProduit": 42, "libelle": "Kéfir Gingembre - 0.0°"}]

    def test_lookup_finds_product(self):
        from common.services.etiquette_palette_service import lookup_product_by_ean
        with patch(
            "common.services.etiquette_palette_service.get_code_barre_matrice",
            return_value=self._mock_matrice(),
        ), patch(
            "common.services.etiquette_palette_service.get_all_products",
            return_value=self._mock_products(),
        ):
            result = lookup_product_by_ean("23770014427018")
        assert result is not None
        assert result["id_produit"] == 42
        assert result["fmt"] == "6x33"
        assert result["pcb"] == 6
        assert result["marque"] == "SYMBIOSE"
        assert result["bottle_type"] == "33cl"
        assert result["gout"] == "Gingembre"
        assert result["designation"] == "Kéfir Gingembre"

    def test_lookup_niko_brand_detection(self):
        """Les libellés NIKO doivent être marqués NIKO."""
        from common.services.etiquette_palette_service import lookup_product_by_ean
        matrice = {
            "produits": [{
                "codesBarres": [{
                    "code": "13770014427325",
                    "modeleProduit": {"idProduit": 99},
                    "modeleContenant": {"contenance": 0.33},
                    "modeleLot": {"libelle": "Carton de 12"},
                }],
            }],
        }
        products = [{"idProduit": 99, "libelle": "NIKO - Kéfir Gingembre - 0.0°"}]
        with patch(
            "common.services.etiquette_palette_service.get_code_barre_matrice",
            return_value=matrice,
        ), patch(
            "common.services.etiquette_palette_service.get_all_products",
            return_value=products,
        ):
            result = lookup_product_by_ean("13770014427325")
        assert result is not None
        assert result["marque"] == "NIKO"

    def test_lookup_not_found_returns_none(self):
        from common.services.etiquette_palette_service import lookup_product_by_ean
        with patch(
            "common.services.etiquette_palette_service.get_code_barre_matrice",
            return_value=self._mock_matrice(),
        ), patch(
            "common.services.etiquette_palette_service.get_all_products",
            return_value=self._mock_products(),
        ):
            result = lookup_product_by_ean("99999999999999")
        assert result is None

    def test_lookup_handles_easybeer_error(self):
        """Si EasyBeer down, retourne None plutôt que de crasher."""
        from common.easybeer import EasyBeerError
        from common.services.etiquette_palette_service import lookup_product_by_ean
        with patch(
            "common.services.etiquette_palette_service.get_code_barre_matrice",
            side_effect=EasyBeerError("transport down"),
        ):
            result = lookup_product_by_ean("23770014427018")
        assert result is None


# ─── save_label_history & list_recent_labels (pipeline DB mocké) ────────────

class TestHistoryPipeline:

    def test_save_passes_all_columns(self):
        """Vérifie que le service insère bien toutes les colonnes attendues
        dans la table (anti-régression si on ajoute une colonne et qu'on
        oublie de mettre à jour save_label_history)."""
        captured: dict = {}

        def _fake_run_sql(query, params=None):
            captured["query"] = query
            captured["params"] = params or {}
            return [{"id": 42}]

        with patch(
            "common.services.etiquette_palette_service.run_sql",
            side_effect=_fake_run_sql,
        ):
            new_id = save_label_history(
                "tenant-x", user_email="op@test.fr",
                ean="03770014427250", lot="110527",
                ddm=_dt.date(2027, 5, 11),
                fmt="6x75", marque="SYMBIOSE",
                designation="Kéfir Pamplemousse Rose",
                gout="Pamplemousse Rose",
                case_count=96, full_pallet=True,
                n_copies=2, pcb=6,
                gtin_uvc="3770014427014",
                code_interne="SK-KDF-PAMP-75",
                bio=True,
            )

        assert new_id == 42
        # Toutes les colonnes nouvellement ajoutées doivent être dans les params
        params = captured["params"]
        assert params["uvc"] == "3770014427014"
        assert params["ci"] == "SK-KDF-PAMP-75"
        assert params["bio"] is True
        assert params["t"] == "tenant-x"
        assert params["ean"] == "03770014427250"
        assert params["m"] == "SYMBIOSE"

    def test_save_failure_returns_none_not_raise(self):
        """Fire-and-forget : si la DB est down, le service ne propage pas."""
        with patch(
            "common.services.etiquette_palette_service.run_sql",
            side_effect=RuntimeError("DB down"),
        ):
            result = save_label_history(
                "tenant-x", user_email="x", ean="0", lot="L",
                ddm=_dt.date(2026, 1, 1), fmt="6x33", marque="SYMBIOSE",
                designation="X", gout="G", case_count=1, full_pallet=True,
                n_copies=1, pcb=6,
            )
        assert result is None  # pas d'exception propagée

    def test_list_recent_parses_all_columns(self):
        """Vérifie que list_recent_labels lit bien gtin_uvc / code_interne / bio."""
        from datetime import datetime as _dtt
        with patch(
            "common.services.etiquette_palette_service.run_sql",
            return_value=[{
                "id": 1, "ean": "03770014427250", "lot": "110527",
                "ddm": _dt.date(2027, 5, 11), "fmt": "6x75",
                "marque": "SYMBIOSE", "designation": "Pamplemousse Rose",
                "gout": "Pamplemousse Rose", "case_count": 96,
                "full_pallet": True, "n_copies": 2, "pcb": 6,
                "gtin_uvc": "3770014427014",
                "code_interne": "SK-KDF-PAMP-75", "bio": True,
                "user_email": "op@test.fr",
                "generated_at": _dtt(2026, 5, 9, 10, 30),
            }],
        ):
            entries = list_recent_labels("tenant-x", limit=10)
        assert len(entries) == 1
        e = entries[0]
        assert e.gtin_uvc == "3770014427014"
        assert e.code_interne == "SK-KDF-PAMP-75"
        assert e.bio is True


# ─── classify_bottle_type — edge cases ──────────────────────────────────────

class TestClassifyBottleEdgeCases:

    def test_unknown_volume_returns_none(self):
        """Format 50cl, 1L hypothétique → pas dans nos 33/75."""
        assert classify_bottle_type("Eau 50cl", "SYMBIOSE", 6, fmt="6x50") is None
        assert classify_bottle_type("Lait 100", "SYMBIOSE", 6, fmt="6x100") is None

    def test_75cl_pcb_8_falls_through(self):
        """Symbiose 8×75 = format exotique non couvert → None."""
        assert classify_bottle_type(
            "Kéfir Test", "SYMBIOSE", 8, fmt="8x75",
        ) is None

    def test_designation_overrides_fmt(self):
        """Si la désignation contient '33cl', priorité sur le fmt."""
        assert classify_bottle_type(
            "Kéfir 33cl Mangue", "SYMBIOSE", 6, fmt="",
        ) == "33cl"

    def test_fmt_only_no_designation(self):
        """fmt seul suffit si la designation ne contient pas le volume."""
        assert classify_bottle_type(
            "Kéfir Mangue Passion", "SYMBIOSE", 6, fmt="6x33",
        ) == "33cl"


# ─── Réimpression bout-en-bout ──────────────────────────────────────────────

class TestReprintIdentical:

    def test_reprint_produces_identical_pdf(self):
        """Une réimpression depuis une HistoryEntry doit générer un PDF
        avec les mêmes données que l'impression initiale."""
        from common.services.etiquette_palette_service import HistoryEntry

        ctx_initial = EtiquetteContext(
            product_label="Kéfir Pamplemousse Rose",
            fmt="6x75", ean13="03770014427250",
            lot="110527", ddm=_dt.date(2027, 5, 11),
            case_count=96, full_pallet=True,
            tenant_name="Symbiose Kéfir", n_copies=2,
            marque="SYMBIOSE", code_interne="SK-KDF-PAMP-75",
            gtin_uvc="3770014427014", pcb=6, bio=True,
        )
        pdf_initial = build_etiquette_palette_pdf(ctx_initial)

        # Simuler le passage par DB et reconstruire depuis HistoryEntry
        h = HistoryEntry(
            id=1, ean="03770014427250", lot="110527",
            ddm=_dt.date(2027, 5, 11), fmt="6x75",
            marque="SYMBIOSE", designation="Kéfir Pamplemousse Rose",
            gout="Pamplemousse Rose", case_count=96, full_pallet=True,
            n_copies=2, pcb=6,
            gtin_uvc="3770014427014",
            code_interne="SK-KDF-PAMP-75", bio=True,
            user_email="op@test.fr",
            generated_at=_dt.datetime.now(),
        )
        ctx_reprint = EtiquetteContext(
            product_label=h.designation or f"GTIN {h.ean}",
            fmt=h.fmt, ean13=h.ean, lot=h.lot, ddm=h.ddm,
            case_count=h.case_count, full_pallet=h.full_pallet,
            tenant_name="Symbiose Kéfir", n_copies=h.n_copies,
            marque=h.marque, code_interne=h.code_interne,
            gtin_uvc=h.gtin_uvc, pcb=h.pcb, bio=h.bio,
        )
        pdf_reprint = build_etiquette_palette_pdf(ctx_reprint)

        # Les contenus textuels doivent être identiques (le timestamp PDF
        # diffère par bytes mais pas le contenu visible).
        r1 = pypdf.PdfReader(io.BytesIO(pdf_initial))
        r2 = pypdf.PdfReader(io.BytesIO(pdf_reprint))
        assert len(r1.pages) == len(r2.pages) == 2

        text1 = r1.pages[0].extract_text()
        text2 = r2.pages[0].extract_text()
        # Les éléments métier doivent être strictement identiques
        for key in ("PAMPLEMOUSSE", "110527", "11/05/2027", "96",
                    "SK-KDF-PAMP-75", "3770014427014", "03770014427250"):
            assert key in text1, f"Initial PDF manque {key!r}"
            assert key in text2, f"Reprint PDF manque {key!r}"


# ─── get_product_image_url ──────────────────────────────────────────────────

class TestProductImageUrl:

    def test_known_flavors(self):
        from common.services.etiquette_palette_service import get_product_image_url
        assert get_product_image_url("Gingembre") == "/assets/GING.jpg"
        assert get_product_image_url("Mangue Passion") == "/assets/MAPA.jpg"
        assert get_product_image_url("Original") == "/assets/ORIG.jpg"

    def test_case_insensitive(self):
        from common.services.etiquette_palette_service import get_product_image_url
        assert get_product_image_url("gingembre") == "/assets/GING.jpg"

    def test_unknown_returns_none(self):
        from common.services.etiquette_palette_service import get_product_image_url
        assert get_product_image_url("Saveur Inconnue") is None

    def test_empty_returns_none(self):
        from common.services.etiquette_palette_service import get_product_image_url
        assert get_product_image_url("") is None
        assert get_product_image_url(None) is None
