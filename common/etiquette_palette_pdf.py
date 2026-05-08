"""
common/etiquette_palette_pdf.py
================================
Génération du PDF d'étiquette palette logistique (102×152 mm, format Dymo 5XL
ou équivalent 4"×6"). Utilise ``python-barcode`` (Code 128) pour le code-barres
et ``fpdf2`` pour la mise en page.

Le contenu encodé est une chaîne d'Application Identifiers GS1 (sans FNC1) :

    (01)<GTIN-14> (15)<YYMMDD> (37)<count, padding 3> (10)<lot>

La chaîne est lisible par toute douchette Code 128 standard. Pour un parsing
GS1-128 strict (avec FNC1), le système qui scanne devra basculer sur un encodeur
spécialisé (treepoem/BWIPP) — voir docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import datetime as _dt
import io
import logging
from dataclasses import dataclass
from pathlib import Path

from barcode import Code128
from barcode.writer import ImageWriter
from fpdf import FPDF

from common.services.etiquette_palette_service import build_gs1_128_payload

_log = logging.getLogger("ferment.etiquette_palette_pdf")


# Format Dymo LabelWriter 5XL Wireless (4"×6")
_LABEL_WIDTH_MM = 102.0
_LABEL_HEIGHT_MM = 152.0
_LABEL_MARGIN_MM = 5.0


# ─── Latin-1 safety (cohérence avec bl_pdf.py / bon_commande_pdf.py) ─────────

_REPLACEMENTS = {
    "—": "-", "–": "-", "‒": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...", " ": " ", " ": " ", " ": " ",
    "œ": "oe", "Œ": "OE", "€": "EUR",
    "×": "x",  # × (multiplication sign) → x ASCII
}


def _txt(s: str) -> str:
    s = str(s or "")
    for k, v in _REPLACEMENTS.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


# ─── API publique ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EtiquetteContext:
    """Données nécessaires au rendu d'une étiquette palette."""
    product_label: str       # ex: "Kéfir Mangue Passion"
    fmt: str                 # ex: "12x33", "6x75"
    ean13: str               # 13 digits
    lot: str                 # ex: "KME27042026"
    ddm: _dt.date            # date de DDM
    case_count: int          # nb total de caisses sur la palette
    full_pallet: bool        # vrai si palette pleine (info indicative)
    tenant_name: str = ""    # ex: "Symbiose Kéfir" — affiché en footer


def build_etiquette_palette_pdf(ctx: EtiquetteContext) -> bytes:
    """Construit le PDF d'étiquette palette (102×152 mm).

    Structure de l'étiquette :
      - Header : nom produit en gros + format
      - Code-barres GS1-128 (Code 128 + AI structurés) + HRI
      - Bloc info : Lot, DDM, Nb caisses, Date impression
      - Footer : tenant
    """
    payload = build_gs1_128_payload(ctx.ean13, ctx.lot, ctx.ddm, ctx.case_count)
    barcode_png = _generate_barcode_png(payload.content)

    pdf = FPDF(orientation="P", unit="mm", format=(_LABEL_WIDTH_MM, _LABEL_HEIGHT_MM))
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(_LABEL_MARGIN_MM, _LABEL_MARGIN_MM, _LABEL_MARGIN_MM)
    pdf.add_page()

    inner_width = _LABEL_WIDTH_MM - 2 * _LABEL_MARGIN_MM

    # ── Header : titre produit + format ─────────────────────────────────
    pdf.set_xy(_LABEL_MARGIN_MM, _LABEL_MARGIN_MM)
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(inner_width, 9, _txt(ctx.product_label.upper()), border=0, align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(inner_width, 7, _txt(f"Format {ctx.fmt}cl"), border=0, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

    pdf.ln(2)
    _hline(pdf, inner_width)

    # ── Code-barres ────────────────────────────────────────────────────
    barcode_y = pdf.get_y() + 3
    barcode_height_mm = 28.0
    pdf.image(io.BytesIO(barcode_png), x=_LABEL_MARGIN_MM, y=barcode_y,
              w=inner_width, h=barcode_height_mm)
    pdf.set_y(barcode_y + barcode_height_mm + 2)

    # HRI (Human Readable Interpretation) sous le code-barres
    pdf.set_font("Courier", "", 8)
    pdf.multi_cell(inner_width, 4, _txt(payload.hri), border=0, align="C")

    pdf.ln(1)
    _hline(pdf, inner_width)

    # ── Bloc info ──────────────────────────────────────────────────────
    pdf.ln(2)
    label_w = 32.0
    value_w = inner_width - label_w
    line_h = 8.0

    rows = [
        ("Lot", ctx.lot),
        ("DDM", ctx.ddm.strftime("%d/%m/%Y")),
        ("Caisses", _format_count(ctx.case_count, ctx.full_pallet)),
        ("Imprimé le", _dt.datetime.now().strftime("%d/%m/%Y à %H:%M")),
    ]
    for label, value in rows:
        pdf.set_x(_LABEL_MARGIN_MM)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(label_w, line_h, _txt(label), border=0, align="L")
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(value_w, line_h, _txt(value), border=0, align="L",
                 new_x="LMARGIN", new_y="NEXT")

    # ── Footer : tenant ─────────────────────────────────────────────────
    pdf.set_y(_LABEL_HEIGHT_MM - _LABEL_MARGIN_MM - 6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(120, 120, 120)
    footer_text = ctx.tenant_name or "Ferment Station"
    pdf.cell(inner_width, 5, _txt(footer_text), border=0, align="C")
    pdf.set_text_color(0, 0, 0)

    out = pdf.output()
    if isinstance(out, str):
        return out.encode("latin-1")
    return bytes(out)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _generate_barcode_png(content: str) -> bytes:
    """Génère un PNG du Code 128 avec ``python-barcode`` (sans HRI intégré)."""
    writer = ImageWriter()
    options = {
        "module_height": 14.0,
        "module_width": 0.30,
        "quiet_zone": 2.0,
        "write_text": False,   # le HRI est dessiné par fpdf2 en dessous
        "background": "white",
        "foreground": "black",
    }
    buf = io.BytesIO()
    Code128(content, writer=writer).write(buf, options=options)
    return buf.getvalue()


def _hline(pdf: FPDF, width: float) -> None:
    """Trait horizontal léger sur toute la largeur intérieure."""
    y = pdf.get_y()
    x = _LABEL_MARGIN_MM
    pdf.set_draw_color(180, 180, 180)
    pdf.line(x, y, x + width, y)
    pdf.set_draw_color(0, 0, 0)


def _format_count(count: int, full_pallet: bool) -> str:
    if full_pallet:
        return f"{count} (palette pleine)"
    return str(count)


# ─── Smoke test (CLI debug only) ────────────────────────────────────────────

if __name__ == "__main__":
    sample = EtiquetteContext(
        product_label="Kéfir Mangue Passion",
        fmt="12x33",
        ean13="3770014427014",
        lot="KME27042026",
        ddm=_dt.date(2027, 4, 27),
        case_count=126,
        full_pallet=True,
        tenant_name="Symbiose Kéfir",
    )
    pdf_bytes = build_etiquette_palette_pdf(sample)
    out = Path("/tmp/etiquette_palette_sample.pdf")
    out.write_bytes(pdf_bytes)
    print(f"OK: {out} ({len(pdf_bytes)} bytes)")
