"""
common/xlsx_fill/bl_pdf.py
==========================
PDF BL enlevements (fpdf2).
"""
from __future__ import annotations

import io
from datetime import date
from typing import List

import pandas as pd

from ._helpers import _load_asset_bytes


def build_bl_enlevements_pdf(
    date_creation: date,
    date_ramasse: date,
    destinataire_title: str,
    destinataire_lines: List[str],
    df_lines: pd.DataFrame,
    *,
    logo_path: str | None = "assets/signature/logo_symbiose.png",
    issuer_name: str = "FERMENT STATION",
    issuer_lines: List[str] | None = None,
    issuer_footer: str | None = "Produits issus de l'Agriculture Biologique certifi\u00e9 par FR-BIO-01",
) -> bytes:
    """PDF BL au look Excel : encadre, tableau gris, totaux. (Helvetica/latin-1)."""
    from fpdf import FPDF

    # ---------- helpers texte latin-1 ----------
    def _latin1_safe(s: str) -> str:
        s = str(s or "")
        repl = {
            "\u2014": "-", "\u2013": "-", "\u2012": "-",
            "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
            "\u2026": "...", "\u00A0": " ", "\u202F": " ", "\u2009": " ",
            "\u0153": "oe", "\u0152": "OE", "\u20ac": "EUR",
        }
        for k, v in repl.items():
            s = s.replace(k, v)
        return s.encode("latin-1", "replace").decode("latin-1")

    def _txt(x) -> str:
        return _latin1_safe(x)

    # ---------- data ----------
    df = df_lines.copy()
    if "Produit" not in df.columns and "Produit (go\u00fbt + format)" in df.columns:
        df = df.rename(columns={"Produit (go\u00fbt + format)": "Produit"})

    def _ival(x):
        try:
            return int(round(float(x)))
        except Exception:
            return 0

    # ---------- PDF ----------
    pdf = FPDF("P", "mm", "A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    left, right = 15, 195
    width = right - left

    # ---- Logo + coordonnees expediteur
    y = 18
    x_text = left
    if logo_path:
        img_bytes = _load_asset_bytes(logo_path)
        if img_bytes:
            bio = io.BytesIO(img_bytes)
            pdf.image(bio, x=left, y=y - 2, w=28)
            x_text = left + 34

    pdf.set_xy(x_text, y)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 6, _txt(issuer_name), ln=1)
    pdf.set_x(x_text)
    pdf.set_font("Helvetica", "", 11)
    if issuer_lines is None:
        issuer_lines = [
            "Carr\u00e9 Ivry B\u00e2timent D2",
            "47 rue Ernest Renan",
            "94200 Ivry-sur-Seine - FRANCE",
            "T\u00e9l : 0967504647",
            "Site : https://www.symbiose-kefir.fr",
        ]
    for line in issuer_lines:
        pdf.set_x(x_text)
        pdf.cell(0, 5, _txt(line), ln=1)
    if issuer_footer:
        pdf.ln(2)
        pdf.set_x(x_text)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 4, _txt(issuer_footer), ln=1)
    pdf.ln(2)

    # ---- Encadre "BON DE LIVRAISON"
    x_box, w_box = left, width * 0.70
    w_lbl, w_val = w_box * 0.55, w_box * 0.45
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_xy(x_box, pdf.get_y() + 2)
    pdf.cell(w_box, 8, _txt("BON DE LIVRAISON"), border=1, ln=1)

    pdf.set_font("Helvetica", "", 11)

    def _row_simple(label: str, value: str):
        pdf.set_x(x_box)
        pdf.cell(w_lbl, 8, _txt(label), border=1)
        pdf.cell(w_val, 8, _txt(value), border=1, ln=1, align="R")

    def _row_dest(label: str, title: str, lines: List[str]):
        val_text = "\n".join([title] + (lines or []))
        n_lines = len(pdf.multi_cell(w_val, 6, _txt(val_text), split_only=True)) or 1
        row_h = max(8, 6 * n_lines)
        y0 = pdf.get_y()
        pdf.set_xy(x_box, y0)
        pdf.cell(w_lbl, row_h, _txt(label), border=1)
        pdf.set_xy(x_box + w_lbl, y0)
        pdf.multi_cell(w_val, 6, _txt(val_text), border=1)
        pdf.set_xy(x_box, y0 + row_h)

    _row_simple("DATE DE CREATION :", date_creation.strftime("%d/%m/%Y"))
    _row_simple("DATE DE RAMASSE :", date_ramasse.strftime("%d/%m/%Y"))
    _row_dest("DESTINATAIRE :", destinataire_title, destinataire_lines)

    # ---- Tableau
    pdf.ln(6)
    pdf.set_fill_color(230, 230, 230)

    headers = ["R\u00e9f\u00e9rence", "Produit", "DDM", "Nb cartons", "Nb palettes", "Poids (kg)"]
    widths_base = [30, 66, 26, 24, 22, 12]
    widths = widths_base[:]
    header_h = 8
    line_h = 6

    pdf.set_font("Helvetica", "B", 10)
    margin_mm = 2.5
    min_w = {0: 30.0, 1: 58.0, 2: 26.0, 3: 22.0, 4: 20.0, 5: 18.0}
    extra_needed = 0.0
    for j, h in enumerate(headers):
        if j == 1:
            continue
        need = pdf.get_string_width(_txt(h)) + 2 * margin_mm
        new_w = max(widths[j], need, min_w.get(j, widths[j]))
        extra_needed += max(0.0, new_w - widths_base[j])
        widths[j] = new_w
    widths[1] = max(min_w[1], widths[1] - extra_needed)
    total = sum(widths)
    if total > 180.0:
        overflow = total - 180.0
        take = min(overflow, max(0.0, widths[1] - min_w[1]))
        widths[1] -= take
        overflow -= take
        for j in (3, 4, 5, 0, 2):
            if overflow <= 0:
                break
            free = max(0.0, widths[j] - min_w[j])
            d = min(free, overflow)
            widths[j] -= d
            overflow -= d

    # En-tete
    x = left
    y = pdf.get_y()
    for h, w in zip(headers, widths):
        pdf.set_xy(x, y)
        pdf.cell(w, header_h, _txt(h), border=1, align="C", fill=True)
        x += w
    pdf.set_xy(left, y + header_h)

    # Lignes
    pdf.set_font("Helvetica", "", 10)
    tot_cart = tot_pal = tot_poids = 0

    def _maybe_break(h):
        if pdf.will_page_break(h + header_h):
            pdf.add_page()
            pdf.set_fill_color(230, 230, 230)
            pdf.set_font("Helvetica", "B", 10)
            xh = left
            yh = pdf.get_y()
            for hh, ww in zip(headers, widths):
                pdf.set_xy(xh, yh)
                pdf.cell(ww, header_h, _txt(hh), border=1, align="C", fill=True)
                xh += ww
            pdf.set_xy(left, yh + header_h)
            pdf.set_font("Helvetica", "", 10)

    for _, r in df.iterrows():
        ref = _txt(r.get("R\u00e9f\u00e9rence", ""))
        prod = _txt(r.get("Produit", ""))
        ddm = _txt(r.get("DDM", ""))
        qc = _ival(r.get("Nb cartons", r.get("Quantit\u00e9 cartons", 0)))
        tot_cart += qc
        qp = _ival(r.get("Nb palettes", r.get("Quantit\u00e9 palettes", 0)))
        tot_pal += qp
        po = _ival(r.get("Poids (kg)", r.get("Poids palettes (kg)", 0)))
        tot_poids += po

        prod_lines = pdf.multi_cell(widths[1], line_h, prod, split_only=True)
        row_h = max(line_h, line_h * len(prod_lines))
        _maybe_break(row_h)

        xrow = left
        yrow = pdf.get_y()
        pdf.set_xy(xrow, yrow)
        pdf.multi_cell(widths[0], row_h, ref, border=1, align="C")
        xrow += widths[0]
        pdf.set_xy(xrow, yrow)
        pdf.multi_cell(widths[1], line_h, prod, border=1, align="L", max_line_height=line_h)
        xrow += widths[1]
        pdf.set_xy(xrow, yrow)
        pdf.multi_cell(widths[2], row_h, ddm, border=1, align="C")
        xrow += widths[2]
        pdf.set_xy(xrow, yrow)
        pdf.multi_cell(widths[3], row_h, str(qc), border=1, align="C")
        xrow += widths[3]
        pdf.set_xy(xrow, yrow)
        pdf.multi_cell(widths[4], row_h, str(qp), border=1, align="C")
        xrow += widths[4]
        pdf.set_xy(xrow, yrow)
        pdf.multi_cell(widths[5], row_h, str(po), border=1, align="C")
        pdf.set_xy(left, yrow + row_h)

    # Totaux
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(widths[0] + widths[1] + widths[2], 8, _txt("Totaux"), border=1, align="R")
    pdf.cell(widths[3], 8, _txt(f"{tot_cart:,}".replace(",", " ")), border=1, align="C")
    pdf.cell(widths[4], 8, _txt(f"{tot_pal:,}".replace(",", " ")), border=1, align="C")
    pdf.cell(widths[5], 8, _txt(f"{tot_poids:,}".replace(",", " ")), border=1, align="C")

    return bytes(pdf.output(dest="S"))
