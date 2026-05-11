"""
common/etiquette_palette_pdf.py
================================
Génération du PDF d'étiquette palette logistique (102×152 mm, format Dymo 5XL
ou équivalent 4"×6"). Utilise ``treepoem`` (wrapper BWIPP) pour générer un
**vrai GS1-128 avec FNC1** lisible par toutes les douchettes logistiques.

Format encodé (Application Identifiers GS1) :

    (02)<GTIN-14>  (15)<YYMMDD>  (10)<lot>  (37)<count>

AI 02 = GTIN des articles contenus dans la palette (les caisses).
AI 15 = Best before date.
AI 10 = Batch / Lot number.
AI 37 = Count of trade items.

Dépendances système : Ghostscript (``apt install ghostscript``) requis par
treepoem pour exécuter BWIPP via PostScript.
"""
from __future__ import annotations

import datetime as _dt
import io
import logging
from dataclasses import dataclass
from pathlib import Path

import treepoem
from fpdf import FPDF

from common.services.etiquette_palette_service import (
    _ean_to_gtin14,
    build_gs1_128_payload,
)

_log = logging.getLogger("ferment.etiquette_palette_pdf")


# Format Brother DK-11247 (4.06" × 6.46" — 103×164 mm) sur Brother
# QL-1110NWBc. Compatible aussi Dymo 4"×6" avec une fine bande blanche.
_LABEL_WIDTH_MM = 103.0
_LABEL_HEIGHT_MM = 164.0
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
    ean13: str               # GTIN colis (carton) — 13 ou 14 digits
    lot: str                 # ex: "KME27042026"
    ddm: _dt.date            # date de DDM
    case_count: int          # nb total de caisses sur la palette
    full_pallet: bool        # vrai si palette pleine (info indicative)
    tenant_name: str = ""    # ex: "Symbiose Kéfir" — affiché en footer
    n_copies: int = 1        # nb d'exemplaires (GS1 recommande 2 : 2 faces)
    marque: str = ""         # "SYMBIOSE" | "NIKO" — pour le logo marque
    code_interne: str = ""   # ex: "SK-KDF-PECHE-75"
    gtin_uvc: str = ""       # GTIN unité-consommateur (bouteille) — 13 digits
    pcb: int = 0             # nb bouteilles par carton
    bio: bool = True         # affiche "*FR_BIO_01" sous le titre si vrai


def build_etiquette_palette_pdf(ctx: EtiquetteContext) -> bytes:
    """Construit le PDF d'étiquette palette (102×152 mm).

    Layout aligné sur le modèle interne « Étiquette Palette.pptx » :
      - Header : « FERMENT STATION » à gauche + logo marque à droite
      - Titre produit en grand (majuscules)
      - Mention bio « *FR_BIO_01 » (si ctx.bio)
      - Bloc de champs alignés à gauche (label en gras + valeur) :
        MARQUE / CODE INTERNE / PCB / QTÉ / LOT / DDM
        GTIN UVC (bouteille) / GTIN COLIS (carton)
      - Code-barres GS1-128 (Code 128 + AI) + HRI

    Si ``ctx.n_copies > 1`` (GS1 : 2 faces de palette), n pages identiques.
    """
    payload = build_gs1_128_payload(ctx.ean13, ctx.lot, ctx.ddm, ctx.case_count)
    barcode_png = _generate_barcode_png(payload.data_with_parens)
    gtin_colis_14 = _ean_to_gtin14(ctx.ean13)
    gtin_uvc = (ctx.gtin_uvc or "").strip()

    pdf = FPDF(orientation="P", unit="mm", format=(_LABEL_WIDTH_MM, _LABEL_HEIGHT_MM))
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(_LABEL_MARGIN_MM, _LABEL_MARGIN_MM, _LABEL_MARGIN_MM)

    inner_width = _LABEL_WIDTH_MM - 2 * _LABEL_MARGIN_MM
    n_copies = max(1, int(ctx.n_copies or 1))

    # Logo marque (chargé une fois)
    repo_root = Path(__file__).resolve().parent.parent
    logo_path: Path | None = None
    if (ctx.marque or "").upper() == "NIKO":
        candidate = repo_root / "assets" / "signature" / "NIKO_Logo.png"
        if candidate.exists():
            logo_path = candidate
    else:
        candidate = repo_root / "assets" / "signature" / "logo_symbiose.png"
        if candidate.exists():
            logo_path = candidate

    for _ in range(n_copies):
        pdf.add_page()

        # ── Header : FERMENT STATION + logo marque à droite ──────────
        pdf.set_xy(_LABEL_MARGIN_MM, _LABEL_MARGIN_MM)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(inner_width / 2, 6, _txt("FERMENT STATION"), border=0, align="L")
        if logo_path is not None:
            try:
                # Logo aligné à droite, hauteur 8 mm
                logo_h = 8.0
                logo_w = 24.0  # largeur max approximative (auto-scaled à H par fpdf)
                pdf.image(
                    str(logo_path),
                    x=_LABEL_WIDTH_MM - _LABEL_MARGIN_MM - logo_w,
                    y=_LABEL_MARGIN_MM,
                    h=logo_h,
                )
            except Exception:
                pass  # logo manquant = on continue sans
        pdf.ln(8)

        _hline(pdf, inner_width)

        # ── Titre produit ─────────────────────────────────────────────
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 14)
        title = f"{ctx.product_label.upper()} {ctx.fmt[-2:]}CL"
        pdf.set_x(_LABEL_MARGIN_MM)
        pdf.multi_cell(inner_width, 6, _txt(title), border=0, align="L")

        if ctx.bio:
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(80, 80, 80)
            pdf.set_x(_LABEL_MARGIN_MM)
            pdf.cell(inner_width, 4, _txt("*FR_BIO_01"), border=0, align="L",
                     new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)

        pdf.ln(1)
        _hline(pdf, inner_width)
        pdf.ln(1.5)

        # ── Bloc de champs (label gras + valeur, modèle PPTX) ────────
        label_w = 32.0
        value_w = inner_width - label_w
        line_h = 5.4

        rows = [
            ("MARQUE",       (ctx.marque or "—").upper()),
            ("CODE INTERNE", ctx.code_interne or "—"),
            ("PCB",          str(ctx.pcb) if ctx.pcb else "—"),
            ("QTÉ",          _format_count(ctx.case_count, ctx.full_pallet)),
            ("LOT",          ctx.lot),
            ("DDM",          ctx.ddm.strftime("%d/%m/%Y")),
            ("GTIN UVC",     gtin_uvc or "—"),
            ("GTIN COLIS",   gtin_colis_14),
        ]
        for label, value in rows:
            pdf.set_x(_LABEL_MARGIN_MM)
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(label_w, line_h, _txt(label + " :"), border=0, align="L")
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(value_w, line_h, _txt(value), border=0, align="L",
                     new_x="LMARGIN", new_y="NEXT")

        # GTIN-128 readable string sous GTIN COLIS — pour que l'opérateur
        # puisse vérifier visuellement le contenu du code-barres sans douchette.
        pdf.set_x(_LABEL_MARGIN_MM)
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(label_w, line_h, _txt("GTIN-128 :"), border=0, align="L")
        pdf.set_font("Courier", "", 7)
        pdf.multi_cell(
            value_w, line_h - 0.6, _txt(payload.data_with_parens),
            border=0, align="L", new_x="LMARGIN", new_y="NEXT",
        )

        pdf.ln(1)
        _hline(pdf, inner_width)

        # ── Code-barres GS1-128 ─────────────────────────────────────
        barcode_y = pdf.get_y() + 2
        barcode_height_mm = 22.0
        pdf.image(io.BytesIO(barcode_png), x=_LABEL_MARGIN_MM, y=barcode_y,
                  w=inner_width, h=barcode_height_mm)
        pdf.set_y(barcode_y + barcode_height_mm + 0.5)

        pdf.set_font("Courier", "", 6.5)
        pdf.multi_cell(inner_width, 3.0, _txt(payload.hri), border=0, align="C")

    out = pdf.output()
    if isinstance(out, str):
        return out.encode("latin-1")
    return bytes(out)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _generate_barcode_png(data_with_parens: str) -> bytes:
    """Génère un PNG du GS1-128 via ``treepoem`` (BWIPP).

    L'argument ``data_with_parens`` est passé tel quel : BWIPP convertit les
    ``(NN)`` en Application Identifiers + FNC1 selon la spec GS1-128.
    """
    img = treepoem.generate_barcode(
        barcode_type="gs1-128",
        data=data_with_parens,
        # parsefnc=True implicite quand on utilise (NN) — BWIPP détecte les AI.
        # On désactive le HRI intégré : il sera dessiné par fpdf2 en dessous
        # avec une typo cohérente.
        options={"includetext": False, "height": 0.6},
    )
    # treepoem retourne une PIL.Image — on la convertit en PNG bytes
    img_rgba = img.convert("RGB") if img.mode != "RGB" else img
    buf = io.BytesIO()
    img_rgba.save(buf, format="PNG")
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
        product_label="Kéfir de Fruits Pêche",
        fmt="6x75",
        ean13="3770014427250",
        lot="160227",
        ddm=_dt.date(2027, 2, 16),
        case_count=96,
        full_pallet=True,
        tenant_name="Symbiose Kéfir",
        marque="SYMBIOSE",
        code_interne="SK-KDF-PECHE-75",
        gtin_uvc="3770014427014",
        pcb=6,
        bio=True,
        n_copies=1,
    )
    pdf_bytes = build_etiquette_palette_pdf(sample)
    out = Path("/tmp/etiquette_palette_sample.pdf")
    out.write_bytes(pdf_bytes)
    print(f"OK: {out} ({len(pdf_bytes)} bytes)")
