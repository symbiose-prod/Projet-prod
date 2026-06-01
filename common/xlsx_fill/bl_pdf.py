"""
common/xlsx_fill/bl_pdf.py
==========================
PDF BL enlevements (fpdf2).
"""
from __future__ import annotations

import io
from datetime import date

import pandas as pd

from ._helpers import _load_asset_bytes


def build_bl_enlevements_pdf(
    date_creation: date,
    date_ramasse: date,
    destinataire_title: str,
    destinataire_lines: list[str],
    df_lines: pd.DataFrame,
    *,
    packaging_lines: list[dict] | None = None,
    logo_path: str | None = "assets/signature/logo_symbiose.png",
    issuer_name: str = "FERMENT STATION",
    issuer_lines: list[str] | None = None,
    issuer_footer: str | None = "Produits issus de l'Agriculture Biologique certifi\u00e9 par FR-BIO-01",
    previous_lines: list[dict] | None = None,
    version: int = 1,
    kind: str = "",
) -> bytes:
    """PDF BL au look Excel : encadre, tableau gris, totaux. (Helvetica/latin-1).

    Mode mise à jour (version > 1 + previous_lines) :
    - Bandeau "MISE À JOUR v{N}" en haut du PDF
    - Lignes ajoutées surlignées en JAUNE (avec marqueur ★ NEW)
    - Lignes modifiées surlignées en BLEU (avec ancien nombre de cartons)
    - Légende en bas du tableau
    """
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

    # Mode d\u00e9taill\u00e9 (1 ligne par SSCC) vs legacy agr\u00e9g\u00e9 (1 ligne par produit).
    # D\u00e9tection par pr\u00e9sence de la colonne SSCC. Mode utilis\u00e9 depuis la refonte
    # Sofripa : R\u00e9f. Sofripa / SSCC / D\u00e9signation / DDM / Lot / Nb cartons / Poids.
    is_detailed_mode = "SSCC" in df.columns

    def _ival(x):
        try:
            return int(round(float(x)))
        except (ValueError, TypeError):
            return 0

    # ---------- Lookup des anciennes lignes (diff vs prévisionnel ou v1) ----------
    # Le diff JAUNE/BLEU s'applique dans 2 cas :
    #   - kind='definitif' avec previous_lines = snapshot du prévisionnel
    #   - legacy (kind vide) avec version > 1 (anciennes ramasses /ramasse)
    is_definitif = (kind == "definitif")
    is_previsionnel = (kind == "previsionnel")
    # BL retroactif : aucun bon n'a pu etre emis lors de la ramasse (douchette
    # HS, oubli de scan...). Reconstitue a posteriori les palettes parties.
    # Se comporte comme un definitif (1 ligne / SSCC) mais sans diff prevu/reel.
    is_retroactif = (kind == "retroactif")
    is_legacy_update = (not kind) and bool(previous_lines) and version > 1
    is_update = (is_definitif and bool(previous_lines)) or is_legacy_update
    old_cartons_by_ref: dict[str, int] = {}
    old_ssccs: set[str] = set()
    if is_update:
        for prev in previous_lines or []:
            ref = str(prev.get("ref") or prev.get("R\u00e9f\u00e9rence") or "").strip()
            if ref:
                old_cartons_by_ref[ref] = _ival(
                    prev.get("cartons")
                    or prev.get("Nb cartons")
                    or prev.get("Quantit\u00e9 cartons")
                    or 0
                )
            sscc_prev = str(prev.get("sscc") or "").strip()
            if sscc_prev:
                old_ssccs.add(sscc_prev)

    def _row_status(ref: str, new_cartons: int, sscc_full: str = "") -> str:
        """Retourne 'added', 'modified' ou 'unchanged'.

        - Mode d\u00e9taill\u00e9 : compare par SSCC. 'modified' impossible (palette unique).
        - Mode legacy : compare par ref agr\u00e9g\u00e9e + cartons.
        - Toujours 'unchanged' hors update.
        """
        if not is_update:
            return "unchanged"
        if is_detailed_mode:
            return "unchanged" if sscc_full in old_ssccs else "added"
        if ref not in old_cartons_by_ref:
            return "added"
        if old_cartons_by_ref[ref] != new_cartons:
            return "modified"
        return "unchanged"

    # ---- Couleurs de surlignage (jaune = nouveau, bleu = modifié) ----
    FILL_ADDED = (255, 243, 205)    # jaune clair
    FILL_MODIFIED = (204, 229, 255)  # bleu clair
    FILL_UNCHANGED = (255, 255, 255)  # blanc
    FILL_HEADER = (230, 230, 230)    # gris (en-tête, totaux)
    FILL_UPDATE_BANNER = (240, 240, 240)  # gris clair neutre (pour ne pas confondre avec jaune=ajout)
    BORDER_UPDATE_BANNER = (200, 40, 40)  # bordure rouge (attention)
    FILL_PREV_BANNER = (217, 234, 254)    # bleu très clair
    BORDER_PREV_BANNER = (37, 99, 235)    # bordure bleue
    FILL_DEF_BANNER = (220, 252, 231)     # vert très clair
    BORDER_DEF_BANNER = (21, 128, 61)     # bordure verte (validation)

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

    # ---- Bandeau d'état (prévisionnel / définitif / legacy mise à jour) ----
    def _draw_banner(title: str, subtitle: str, fill_rgb, border_rgb,
                     subtitle_height: int = 6):
        """Trace un bandeau encadré avec titre gras + sous-titre."""
        pdf.set_fill_color(*fill_rgb)
        pdf.set_draw_color(*border_rgb)
        pdf.set_line_width(0.5)
        pdf.set_x(left)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(width, 9, _txt(title), border=1, ln=1, align="L", fill=True)
        if subtitle:
            pdf.set_x(left)
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(width, subtitle_height, _txt(subtitle),
                     border=1, ln=1, align="L", fill=True)
        pdf.set_line_width(0.2)
        pdf.set_draw_color(0, 0, 0)
        pdf.ln(2)

    if is_previsionnel:
        _draw_banner(
            title="  PREVISIONNEL - pour dimensionnement camion",
            subtitle=(
                "Estimation indicative. Un BL definitif (rectificatif) "
                "sera envoye au moment du chargement."
            ),
            fill_rgb=FILL_PREV_BANNER,
            border_rgb=BORDER_PREV_BANNER,
        )
    elif is_definitif:
        subtitle = (
            "Reflete exactement ce qui part dans le camion."
        )
        if bool(previous_lines):
            subtitle += (
                " Ecart vs previsionnel : nouvelles lignes en JAUNE, "
                "modifiees en BLEU (ancien nb cartons indique)."
            )
        _draw_banner(
            title="  BL DEFINITIF - Rectificatif",
            subtitle=subtitle,
            fill_rgb=FILL_DEF_BANNER,
            border_rgb=BORDER_DEF_BANNER,
        )
    elif is_retroactif:
        _draw_banner(
            title="  BL ETABLI A POSTERIORI",
            subtitle=(
                f"Aucun bon n'a pu etre emis lors de la ramasse du "
                f"{date_ramasse:%d/%m/%Y}. Document recapitulatif des "
                f"palettes effectivement parties."
            ),
            fill_rgb=FILL_UPDATE_BANNER,
            border_rgb=BORDER_UPDATE_BANNER,
            subtitle_height=10,
        )
    elif is_legacy_update:
        _draw_banner(
            title=f"  /!\\  MISE A JOUR - Version {version}",
            subtitle=(
                "Nouvelles lignes surlignees en JAUNE "
                "- Lignes modifiees surlignees en BLEU (ancien nb cartons indique)"
            ),
            fill_rgb=FILL_UPDATE_BANNER,
            border_rgb=BORDER_UPDATE_BANNER,
        )

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

    def _row_dest(label: str, title: str, lines: list[str]):
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

    # Mode d\u00e9taill\u00e9 : 7 colonnes (R\u00e9f Sofripa, SSCC, D\u00e9signation, DDM, Lot,
    # Nb cartons, Poids). La colonne "flex" qui absorbe l'exc\u00e9dent est
    # D\u00e9signation (index 2) car c'est la plus longue \u00e0 priori (libell\u00e9s Sofripa).
    # Mode legacy : 6 colonnes existantes, flex = Produit (index 1).
    if is_detailed_mode:
        headers = ["R\u00e9f. Sofripa", "SSCC", "D\u00e9signation", "DDM", "Lot", "Nb cartons", "Poids (kg)"]
        widths_base = [22, 24, 50, 22, 22, 22, 18]   # total = 180
        flex_idx = 2
        min_w = {0: 22.0, 1: 24.0, 2: 38.0, 3: 22.0, 4: 18.0, 5: 22.0, 6: 18.0}
    else:
        headers = ["R\u00e9f\u00e9rence", "Produit", "DDM", "Nb cartons", "Nb palettes", "Poids (kg)"]
        # En mode mise \u00e0 jour, "Nb cartons" devient "123 (etait 99)" (~24mm),
        # qui ne tient pas dans 24mm \u2192 \u00e9largir la colonne (-6mm sur Produit).
        if is_update:
            widths_base = [30, 60, 26, 30, 22, 12]
        else:
            widths_base = [30, 66, 26, 24, 22, 12]
        flex_idx = 1
        min_w = {0: 30.0, 1: 58.0, 2: 26.0, 3: 28.0 if is_update else 22.0, 4: 20.0, 5: 18.0}
    widths = widths_base[:]
    header_h = 8
    line_h = 6

    pdf.set_font("Helvetica", "B", 10)
    margin_mm = 2.5
    extra_needed = 0.0
    for j, h in enumerate(headers):
        if j == flex_idx:
            continue
        need = pdf.get_string_width(_txt(h)) + 2 * margin_mm
        new_w = max(widths[j], need, min_w.get(j, widths[j]))
        extra_needed += max(0.0, new_w - widths_base[j])
        widths[j] = new_w
    widths[flex_idx] = max(min_w[flex_idx], widths[flex_idx] - extra_needed)
    total = sum(widths)
    if total > 180.0:
        overflow = total - 180.0
        take = min(overflow, max(0.0, widths[flex_idx] - min_w[flex_idx]))
        widths[flex_idx] -= take
        overflow -= take
        # Autres colonnes peuvent c\u00e9der dans l'ordre de priorit\u00e9 (num\u00e9riques en dernier)
        shrink_order = (5, 6, 0, 2, 3, 4) if is_detailed_mode else (3, 4, 5, 0, 2)
        for j in shrink_order:
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

    added_count = 0
    modified_count = 0

    for _, r in df.iterrows():
        ref_raw = str(r.get("R\u00e9f. Sofripa", r.get("R\u00e9f\u00e9rence", ""))).strip()
        ref = _txt(ref_raw)
        ddm = _txt(r.get("DDM", ""))
        qc = _ival(r.get("Nb cartons", r.get("Quantit\u00e9 cartons", 0)))
        tot_cart += qc
        po = _ival(r.get("Poids (kg)", r.get("Poids palettes (kg)", 0)))
        tot_poids += po

        if is_detailed_mode:
            sscc_display = str(r.get("SSCC", "")).strip()
            desig = _txt(r.get("D\u00e9signation", ""))
            lot = _txt(r.get("Lot", ""))
            tot_pal += 1   # 1 ligne = 1 palette
        else:
            sscc_display = ""
            desig = _txt(r.get("Produit", ""))
            lot = ""
            qp = _ival(r.get("Nb palettes", r.get("Quantit\u00e9 palettes", 0)))
            tot_pal += qp

        # Statut de la ligne pour le surlignage. En mode détaillé : compare
        # par suffixe 8 digits du SSCC (previous_lines peuvent contenir le
        # SSCC complet ou tronqué, on aligne sur les 8 derniers).
        if is_detailed_mode and is_update:
            old_ssccs_suffix = {s[-8:] for s in old_ssccs}
            status = "unchanged" if sscc_display[-8:] in old_ssccs_suffix else "added"
        else:
            status = _row_status(ref_raw, qc)
        if status == "added":
            fill_rgb = FILL_ADDED
            added_count += 1
            ref_display = _txt(f"* {ref_raw}")  # marqueur "nouveau"
            cart_display = str(qc) if is_detailed_mode else f"{qc} (NEW)"
        elif status == "modified":
            fill_rgb = FILL_MODIFIED
            modified_count += 1
            old_c = old_cartons_by_ref.get(ref_raw, 0)
            ref_display = ref
            cart_display = f"{qc} (etait {old_c})"
        else:
            fill_rgb = FILL_UNCHANGED
            ref_display = ref
            cart_display = str(qc)

        # Suffix "kg" sur le poids en mode détaillé (demande Sofripa)
        po_display = f"{po} kg" if is_detailed_mode else str(po)

        # Hauteur de ligne = max wrap des cellules.
        if is_detailed_mode:
            cells_data = [
                (widths[0], ref_display,  "C"),
                (widths[1], sscc_display, "C"),
                (widths[2], desig,        "L"),
                (widths[3], ddm,          "C"),
                (widths[4], lot,          "C"),
                (widths[5], cart_display, "C"),
                (widths[6], po_display,   "C"),
            ]
        else:
            cells_data = [
                (widths[0], ref_display,  "C"),
                (widths[1], desig,        "L"),
                (widths[2], ddm,          "C"),
                (widths[3], cart_display, "C"),
                (widths[4], str(qp),      "C"),
                (widths[5], po_display,   "C"),
            ]
        n_lines_per_cell = [
            max(1, len(pdf.multi_cell(w, line_h, t, split_only=True)))
            for (w, t, _a) in cells_data
        ]
        n_max = max(n_lines_per_cell)
        row_h = line_h * n_max
        _maybe_break(row_h)

        # Applique la couleur de fond pour toutes les cellules de cette ligne
        use_fill = status != "unchanged"
        if use_fill:
            pdf.set_fill_color(*fill_rgb)

        xrow = left
        yrow = pdf.get_y()
        for (w, txt, align), n_own in zip(cells_data, n_lines_per_cell):
            pdf.set_xy(xrow, yrow)
            pdf.multi_cell(w, line_h, txt, border=1, align=align,
                           max_line_height=line_h, fill=use_fill)
            if n_own < n_max:
                # Padding pour aligner les cellules courtes sur row_h
                pdf.set_xy(xrow, yrow + n_own * line_h)
                pdf.cell(w, (n_max - n_own) * line_h, "", border=1, fill=use_fill)
            xrow += w
        pdf.set_xy(left, yrow + row_h)

    # Totaux — reset couleur grise pour cohérence avec en-tête
    pdf.set_fill_color(*FILL_HEADER)
    pdf.set_font("Helvetica", "B", 10)
    if is_detailed_mode:
        # 7 cols : Réf / SSCC / Désignation / DDM / Lot / Cartons / Poids
        # Le label "Totaux (N palettes)" remplace une colonne "Nb palettes"
        # absente : on indique le nombre de palettes dans le label.
        label = f"Totaux ({tot_pal} palette{'s' if tot_pal > 1 else ''})"
        label_w = sum(widths[:5])
        pdf.cell(label_w, 8, _txt(label), border=1, align="R", fill=True)
        pdf.cell(widths[5], 8, _txt(f"{tot_cart:,}".replace(",", " ")), border=1, align="C", fill=True)
        pdf.cell(widths[6], 8, _txt(f"{tot_poids:,} kg".replace(",", " ")), border=1, align="C", fill=True)
    else:
        pdf.cell(widths[0] + widths[1] + widths[2], 8, _txt("Totaux"), border=1, align="R", fill=True)
        pdf.cell(widths[3], 8, _txt(f"{tot_cart:,}".replace(",", " ")), border=1, align="C", fill=True)
        pdf.cell(widths[4], 8, _txt(f"{tot_pal:,}".replace(",", " ")), border=1, align="C", fill=True)
        pdf.cell(widths[5], 8, _txt(f"{tot_poids:,}".replace(",", " ")), border=1, align="C", fill=True)
    pdf.ln()

    # ---- Légende différentielle (uniquement en mode mise à jour) ----
    if is_update and (added_count + modified_count) > 0:
        pdf.ln(3)
        pdf.set_x(left)
        pdf.set_font("Helvetica", "", 9)
        if is_detailed_mode:
            # En mode détaillé, pas de "modifiée" possible (palette unique).
            # Le récap parle de palettes au lieu de lignes (vocabulaire métier).
            pdf.cell(0, 5, _txt(
                f"Recapitulatif : {added_count} palette(s) ajoutee(s) "
                f"par rapport au previsionnel."
            ), ln=1)
            pdf.set_x(left)
            pdf.set_fill_color(*FILL_ADDED)
            pdf.cell(6, 5, "", border=1, fill=True)
            pdf.cell(50, 5, _txt(" Palette ajoutee au definitif"), ln=1)
        else:
            pdf.cell(0, 5, _txt(
                f"Recapitulatif de la mise a jour : {added_count} ligne(s) ajoutee(s), "
                f"{modified_count} ligne(s) modifiee(s)."
            ), ln=1)
            # Petite légende visuelle
            pdf.set_x(left)
            pdf.set_fill_color(*FILL_ADDED)
            pdf.cell(6, 5, "", border=1, fill=True)
            pdf.cell(35, 5, _txt(" Nouvelle ligne"), ln=0)
            pdf.set_fill_color(*FILL_MODIFIED)
            pdf.cell(6, 5, "", border=1, fill=True)
            pdf.cell(40, 5, _txt(" Ligne modifiee"), ln=1)

    # ---- Section Emballages à récupérer (optionnel)
    if packaging_lines:
        pdf.ln(10)

        table_w = sum(widths)
        pkg_widths = [table_w * 0.55, table_w * 0.25, table_w * 0.20]

        # Sous-titre
        _maybe_break(20)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_fill_color(245, 245, 245)
        pdf.set_x(left)
        pdf.cell(sum(pkg_widths), 8, _txt("EMBALLAGES A RECUPERER"), border=1, align="L", fill=True)
        pdf.ln()

        # En-têtes colonnes
        pkg_headers = ["Designation", "Quantite", "Unite"]
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_fill_color(230, 230, 230)
        pdf.set_x(left)
        for h, w in zip(pkg_headers, pkg_widths):
            pdf.cell(w, header_h, _txt(h), border=1, align="C", fill=True)
        pdf.ln()

        # Lignes
        pdf.set_font("Helvetica", "", 10)
        for pl in packaging_lines:
            _maybe_break(line_h)
            pdf.set_x(left)
            pdf.cell(pkg_widths[0], line_h, _txt(pl.get("label", "")), border=1, align="L")
            pdf.cell(pkg_widths[1], line_h, str(pl.get("qty", 0)), border=1, align="C")
            pdf.cell(pkg_widths[2], line_h, _txt(pl.get("unit", "")), border=1, align="C")
            pdf.ln()

    return bytes(pdf.output(dest="S"))
