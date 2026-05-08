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
    extract_ean_from_image,
    extract_gs1_data_from_image,
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
