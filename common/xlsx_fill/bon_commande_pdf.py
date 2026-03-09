"""
common/xlsx_fill/bon_commande_pdf.py
=====================================
PDF Bon de Commande fournisseur — charte Ferment Station (fpdf2).

Follows the same fpdf2 pattern as bl_pdf.py:
- Helvetica font (built-in, no TTF)
- Latin-1 safe encoding
- Logo loading from assets
"""
from __future__ import annotations

import io
import logging
from datetime import date
from typing import Any

from ._helpers import _load_asset_bytes

_log = logging.getLogger("ferment.xlsx_fill")


# ─── Latin-1 safety (same as bl_pdf.py) ──────────────────────────────────────

_REPLACEMENTS = {
    "\u2014": "-", "\u2013": "-", "\u2012": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2026": "...", "\u00A0": " ", "\u202F": " ", "\u2009": " ",
    "\u0153": "oe", "\u0152": "OE", "\u20ac": "EUR",
}


def _latin1_safe(s: str) -> str:
    s = str(s or "")
    for k, v in _REPLACEMENTS.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


_txt = _latin1_safe


def _fmt_number(n: int | float) -> str:
    """French-style thousand separator: 50 540."""
    return f"{int(n):,}".replace(",", " ")


# ─── Public API ──────────────────────────────────────────────────────────────


def build_bon_commande_pdf(
    order_data: dict[str, Any],
    supplier_info: dict[str, Any],
    *,
    logo_path: str | None = "assets/signature/logo_symbiose.png",
    issuer_name: str = "FERMENT STATION",
    issuer_lines: list[str] | None = None,
) -> bytes:
    """Build a Purchase Order PDF (Bon de Commande).

    Args:
        order_data: dict with keys:
            - reference: str (e.g. "BC-2026-0308-WIEGAND")
            - date: date
            - items: list[dict] with label, pallets, qty, conditionnement
            - delivery_date: str (requested delivery date text)
            - notes: str | None (optional free-text notes)
        supplier_info: dict with keys:
            - name: str
            - address_lines: list[str]
            - contact_name: str | None
            - email: str | None

    Returns:
        PDF file as bytes.
    """
    from fpdf import FPDF

    pdf = FPDF("P", "mm", "A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    left = 15.0
    right = 195.0
    width = right - left

    # ── Logo + issuer header ──────────────────────────────────────────────
    y = 18.0
    x_text = left
    if logo_path:
        img_bytes = _load_asset_bytes(logo_path)
        if img_bytes:
            bio = io.BytesIO(img_bytes)
            pdf.image(bio, x=left, y=y - 2, w=28)
            x_text = left + 34

    pdf.set_xy(x_text, y)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 6, _txt(issuer_name), ln=1)
    pdf.set_font("Helvetica", "", 9)
    if issuer_lines is None:
        issuer_lines = [
            "Carre Ivry Batiment D2",
            "47 rue Ernest Renan",
            "94200 Ivry-sur-Seine - FRANCE",
            "Tel : 09 67 50 46 47",
            "hello@symbiose-kefir.fr",
        ]
    for line in issuer_lines:
        pdf.set_x(x_text)
        pdf.cell(0, 4.5, _txt(line), ln=1)

    pdf.ln(10)

    # ── Title ──────────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(width, 10, _txt("BON DE COMMANDE"), ln=1, align="C")
    pdf.ln(2)

    # ── Thin horizontal rule ──
    pdf.set_draw_color(200, 200, 200)
    pdf.line(left, pdf.get_y(), right, pdf.get_y())
    pdf.ln(4)

    # ── Reference + Date ──────────────────────────────────────────────────
    ref = order_data.get("reference", "")
    order_date = order_data.get("date") or date.today()

    pdf.set_font("Helvetica", "", 10)
    pdf.cell(width / 2, 6, _txt(f"Reference : {ref}"))
    pdf.cell(width / 2, 6, _txt(f"Date : {order_date.strftime('%d/%m/%Y')}"),
             align="R", ln=1)
    pdf.ln(6)

    # ── Two-column info: Issuer | Supplier ─────────────────────────────
    col_w = width / 2 - 5
    y_start = pdf.get_y()

    # Left column: "EMETTEUR" (issuer summary)
    pdf.set_xy(left, y_start)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(245, 245, 245)
    pdf.cell(col_w, 6, _txt("  EMETTEUR"), ln=1, fill=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_x(left)
    pdf.cell(col_w, 5, _txt(issuer_name), ln=1)
    for line in (issuer_lines or [])[:3]:
        pdf.set_x(left)
        pdf.cell(col_w, 5, _txt(line), ln=1)

    y_after_left = pdf.get_y()

    # Right column: "FOURNISSEUR"
    pdf.set_xy(left + col_w + 10, y_start)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(col_w, 6, _txt("  FOURNISSEUR"), ln=1, fill=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_x(left + col_w + 10)
    pdf.cell(col_w, 5, _txt(supplier_info.get("name", "")), ln=1)
    for line in supplier_info.get("address_lines", []):
        pdf.set_x(left + col_w + 10)
        pdf.cell(col_w, 5, _txt(line), ln=1)
    contact = supplier_info.get("contact_name")
    if contact:
        pdf.set_x(left + col_w + 10)
        pdf.cell(col_w, 5, _txt(f"Contact : {contact}"), ln=1)
    email = supplier_info.get("email")
    if email:
        pdf.set_x(left + col_w + 10)
        pdf.cell(col_w, 5, _txt(f"Email : {email}"), ln=1)

    y_after_right = pdf.get_y()
    pdf.set_y(max(y_after_left, y_after_right) + 6)

    # ── Delivery date ────────────────────────────────────────────────────
    delivery = order_data.get("delivery_date", "")
    if delivery:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(width, 6,
                 _txt(f"Date de livraison souhaitee : {delivery}"), ln=1)
        pdf.ln(4)

    # ── Items table ──────────────────────────────────────────────────────
    col_widths = [70, 28, 38, 44]  # total = 180
    _ou = order_data.get("order_unit", "palette").capitalize() + "s"
    _qu = order_data.get("qty_unit", "unités").capitalize()
    headers = ["Reference", _txt(_ou), _txt(_qu), "Conditionnement"]

    # Header row
    pdf.set_fill_color(55, 65, 81)   # dark gray
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 10)
    for h, w in zip(headers, col_widths):
        pdf.cell(w, 8, _txt(h), border=0, align="C", fill=True)
    pdf.ln()
    pdf.set_text_color(0, 0, 0)

    # Data rows
    items = order_data.get("items") or []
    pdf.set_font("Helvetica", "", 10)
    fill_alt = False
    for item in items:
        if fill_alt:
            pdf.set_fill_color(248, 248, 248)
        else:
            pdf.set_fill_color(255, 255, 255)

        pdf.cell(col_widths[0], 7, _txt(f"  {item.get('label', '')}"),
                 border=0, fill=True)
        pdf.cell(col_widths[1], 7, _txt(str(item.get("pallets", ""))),
                 border=0, align="C", fill=True)
        pdf.cell(col_widths[2], 7, _txt(_fmt_number(item.get("qty", 0))),
                 border=0, align="C", fill=True)
        pdf.cell(col_widths[3], 7, _txt(item.get("conditionnement", "")),
                 border=0, align="C", fill=True)
        pdf.ln()
        fill_alt = not fill_alt

    # Thin separator line
    pdf.set_draw_color(200, 200, 200)
    pdf.line(left, pdf.get_y(), right, pdf.get_y())
    pdf.ln(1)

    # Total row
    total_pal = sum(it.get("pallets", 0) for it in items)
    total_qty = sum(it.get("qty", 0) for it in items)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(245, 245, 245)
    pdf.cell(col_widths[0], 8, _txt("  TOTAL"), fill=True)
    pdf.cell(col_widths[1], 8, _txt(str(total_pal)),
             align="C", fill=True)
    pdf.cell(col_widths[2], 8, _txt(_fmt_number(total_qty)),
             align="C", fill=True)
    pdf.cell(col_widths[3], 8, "", fill=True)
    pdf.ln()

    # ── Notes ────────────────────────────────────────────────────────────
    notes = order_data.get("notes")
    if notes:
        pdf.ln(6)
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(100, 100, 100)
        pdf.multi_cell(width, 5, _txt(f"Note : {notes}"))
        pdf.set_text_color(0, 0, 0)

    # ── Footer ───────────────────────────────────────────────────────────
    pdf.ln(12)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(left, pdf.get_y(), right, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(width, 4,
             _txt("Ferment Station — Producteur de boissons fermentees bio"),
             align="C", ln=1)
    pdf.cell(width, 4,
             _txt("Carre Ivry Bat. D2, 47 rue Ernest Renan, "
                  "94200 Ivry-sur-Seine"),
             align="C", ln=1)
    pdf.cell(width, 4,
             _txt("Produits issus de l'Agriculture Biologique "
                  "certifies par FR-BIO-01"),
             align="C", ln=1)

    return bytes(pdf.output(dest="S"))
