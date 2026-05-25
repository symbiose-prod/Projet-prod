"""
common/production_sheet_pdf.py
===============================
Génération PDF d'une fiche de production finalisée.

Reproduit la fiche Excel papier (`Fiche de production` PROD_EN_002 v07) en
format A4 portrait multi-page. Layout :

  1. En-tête : produit + cuve + DDM + lot
  2. Tableau Fermentation (date, heure, Brix, pH, °C, goût, observation, matricule)
     + statut Conforme/NC
  3. Phase 1 Dilution : recette (kg) + paramètres cuve
  4. Phase 2 Filtration / Phase 2 Remplissage
  5. Conditionnement prévu (tableau)
  6. Conditionnement réel (tableau) — source SSCC indiquée si applicable
  7. Répartition (Antoine / Échantillons / Traçabilité)
  8. Remarques (texte libre)
  9. Incidents (notes + photos)
  10. Footer : opérateur + date de génération

PDF généré via FPDF (cohérence avec common/xlsx_fill/bl_pdf et étiquettes
palette). Photos décodées depuis base64 OU téléchargées depuis OVH Object
Storage (via la clé S3, après migration Phase B), puis insérées dans le PDF
via fichiers temporaires.
"""
from __future__ import annotations

import base64
import datetime as _dt
import logging
import tempfile
from pathlib import Path
from typing import Any

_log = logging.getLogger("ferment.production_sheet_pdf")


# ─── Helpers texte latin-1 (FPDF natif est limité à cp1252) ────────────────

def _latin1(s: Any) -> str:
    """Convertit en string latin-1 safe (FPDF natif). Garde l'esprit du texte."""
    s = str(s) if s is not None else ""
    repl = {
        "—": "-", "–": "-", "‒": "-",
        "‘": "'", "’": "'", "“": '"', "”": '"',
        "…": "...", " ": " ", " ": " ", " ": " ",
        "œ": "oe", "Œ": "OE", "€": "EUR",
    }
    for k, v in repl.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


# ─── Layout constantes ─────────────────────────────────────────────────────

_PAGE_MARGIN = 12      # mm
_SECTION_GAP = 5       # mm entre sections
_LINE_H = 5            # mm hauteur de ligne standard

# Couleurs (RGB tuples)
_COLOR_INK = (17, 24, 39)
_COLOR_MUTED = (107, 114, 128)
_COLOR_GREEN = (21, 128, 61)
_COLOR_ORANGE = (249, 115, 22)
_COLOR_BORDER = (229, 231, 235)
_COLOR_BG_LIGHT = (250, 250, 247)


# ─── Builder principal ──────────────────────────────────────────────────────

def build_production_sheet_pdf(sheet) -> bytes:
    """Génère le PDF d'une fiche de production finalisée.

    Args:
        sheet: instance ``ProductionSheetDetail`` (depuis production_sheet_service).

    Returns:
        bytes du PDF généré.
    """
    from fpdf import FPDF

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=_PAGE_MARGIN)
    pdf.add_page()
    pdf.set_margins(_PAGE_MARGIN, _PAGE_MARGIN, _PAGE_MARGIN)

    _draw_header(pdf, sheet)
    _section_meta(pdf, sheet)
    _section_fermentation(pdf, sheet)
    _section_dilution(pdf, sheet)
    _section_filtration_remplissage(pdf, sheet)
    _section_conditionnement(pdf, sheet, key="conditionnement_prevu",
                             title="Conditionnement prevu")
    _section_conditionnement(pdf, sheet, key="conditionnement_reel",
                             title="Conditionnement reel", with_sscc_meta=True)
    _section_repartition(pdf, sheet)
    _section_remarques(pdf, sheet)
    _section_incidents(pdf, sheet)
    _draw_footer(pdf, sheet)

    raw = pdf.output(dest="S")
    if isinstance(raw, str):
        raw = raw.encode("latin-1")
    return bytes(raw)


# ─── En-tête + footer ──────────────────────────────────────────────────────

def _draw_header(pdf, sheet) -> None:
    """En-tête PDF : titre + référence document."""
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*_COLOR_INK)
    pdf.cell(0, 8, _latin1("Fiche de production"), ln=True)

    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*_COLOR_MUTED)
    today = _dt.date.today().strftime("%d/%m/%Y")
    pdf.cell(0, 4, _latin1(f"Code : PROD_EN_002  -  Version : 07  -  Genere le {today}"), ln=True)
    pdf.ln(2)


def _draw_footer(pdf, sheet) -> None:
    """Footer compact : opérateur + date finalisation."""
    pdf.ln(4)
    pdf.set_draw_color(*_COLOR_BORDER)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*_COLOR_MUTED)
    operator = getattr(sheet, "created_by_email", "") or ""
    finalized = _dt.datetime.now().strftime("%d/%m/%Y %H:%M")
    pdf.cell(0, 4, _latin1(f"Operateur : {operator}    |    Finalisee : {finalized}"), ln=True)


# ─── Helpers section ───────────────────────────────────────────────────────

def _section_title(pdf, title: str) -> None:
    pdf.ln(_SECTION_GAP)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*_COLOR_INK)
    pdf.cell(0, 6, _latin1(title), ln=True)
    pdf.set_draw_color(*_COLOR_BORDER)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(1)


def _kv_row(pdf, label: str, value: str, label_w: float = 50) -> None:
    """Ligne "Label : valeur" — utilisée dans Méta, Phase 1, etc."""
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_COLOR_MUTED)
    pdf.cell(label_w, _LINE_H, _latin1(label), ln=False)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*_COLOR_INK)
    pdf.cell(0, _LINE_H, _latin1(value or "-"), ln=True)


def _fmt_num(v, decimals: int = 2, unit: str = "") -> str:
    """Format un float/int proprement : pas de décimales inutiles, unité optionnelle."""
    if v is None:
        return "-"
    try:
        fv = float(v)
    except (ValueError, TypeError):
        return str(v)
    if fv == int(fv):
        s = f"{int(fv)}"
    else:
        s = f"{fv:.{decimals}f}".rstrip("0").rstrip(".")
    return f"{s} {unit}".strip() if unit else s


# ─── Méta ──────────────────────────────────────────────────────────────────

def _section_meta(pdf, sheet) -> None:
    _section_title(pdf, "Identification")
    _kv_row(pdf, "Produit", sheet.produit or "-")
    _kv_row(pdf, "Cuve", sheet.cuve or "-")
    ddm = sheet.ddm.strftime("%d/%m/%Y") if sheet.ddm else "-"
    _kv_row(pdf, "DDM", ddm)
    _kv_row(pdf, "Lot", sheet.lot or "-")
    if sheet.brassin_id:
        _kv_row(pdf, "Brassin EasyBeer", sheet.brassin_id)


# ─── Fermentation ──────────────────────────────────────────────────────────

def _section_fermentation(pdf, sheet) -> None:
    _section_title(pdf, "Fermentation")
    section = (sheet.data or {}).get("fermentation") or {}
    mesures = section.get("mesures") or []
    statut = section.get("statut") or ""

    # Tableau colonnes : Date | Heure | Brix | pH | T°C | Gout | Observation | Matricule
    col_widths = [22, 14, 14, 14, 14, 16, 60, 22]
    headers = ["Date", "Heure", "Brix", "pH", "T degC", "Gout", "Observation", "Matricule"]

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_COLOR_MUTED)
    pdf.set_fill_color(*_COLOR_BG_LIGHT)
    for w, h in zip(col_widths, headers):
        pdf.cell(w, 6, _latin1(h), border=1, fill=True, ln=False)
    pdf.ln(6)

    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*_COLOR_INK)
    if not mesures:
        # ligne vide pour indiquer "à remplir"
        pdf.cell(sum(col_widths), 6, _latin1("Aucune mesure."), border=1, ln=True)
    else:
        for m in mesures:
            row = [
                m.get("date") or "-",
                m.get("heure") or "-",
                _fmt_num(m.get("brix")),
                _fmt_num(m.get("ph")),
                _fmt_num(m.get("temperature")),
                m.get("gout") or "-",
                (m.get("observation") or "")[:80],
                m.get("matricule") or "-",
            ]
            for w, val in zip(col_widths, row):
                pdf.cell(w, 6, _latin1(val), border=1, ln=False)
            pdf.ln(6)

    pdf.ln(2)
    pdf.set_font("Helvetica", "", 9)
    color = _COLOR_GREEN if statut == "Conforme" else (
        _COLOR_ORANGE if statut == "Non conforme" else _COLOR_MUTED
    )
    pdf.set_text_color(*color)
    pdf.cell(0, 5, _latin1(f"Statut : {statut or 'A evaluer'}"), ln=True)


# ─── Phase 1 Dilution ──────────────────────────────────────────────────────

def _section_dilution(pdf, sheet) -> None:
    _section_title(pdf, "Phase 1 - Dilution")
    d = (sheet.data or {}).get("dilution") or {}

    # Recette
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*_COLOR_INK)
    pdf.cell(0, 5, _latin1("Recette (kg)"), ln=True)
    _kv_row(pdf, "Sucre", _fmt_num(d.get("sucre_kg"), unit="kg"))
    _kv_row(pdf, "Figues", _fmt_num(d.get("figues_kg"), unit="kg"))
    _kv_row(pdf, "Jus de citron", _fmt_num(d.get("jus_citron_kg"), unit="kg"))
    _kv_row(pdf, "Grains de Kefir", _fmt_num(d.get("grains_kg"), unit="kg"))
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 5, _latin1("Parametres cuve"), ln=True)
    _kv_row(pdf, "Volume de remplissage", _fmt_num(d.get("volume_remplissage_l"), unit="L"))
    _kv_row(pdf, "Niveau de liquide", _fmt_num(d.get("niveau_liquide_cm"), unit="cm"))
    _kv_row(pdf, "Pression bulleur", _fmt_num(d.get("pression_bulleur_bars"), unit="bars"))
    _kv_row(pdf, "Temperature de cuve", _fmt_num(d.get("temperature_cuve_c"), unit="degC"))


# ─── Phase 2 Filtration + Remplissage ──────────────────────────────────────

def _section_filtration_remplissage(pdf, sheet) -> None:
    f = (sheet.data or {}).get("filtration") or {}
    r = (sheet.data or {}).get("remplissage") or {}

    _section_title(pdf, "Phase 2 - Filtration")
    _kv_row(pdf, "Volume filtre", _fmt_num(f.get("volume_filtre_l"), unit="L"))
    _kv_row(pdf, "Volume final", _fmt_num(f.get("volume_final_l"), unit="L"))
    _kv_row(pdf, "Hauteur", _fmt_num(f.get("hauteur_cm"), unit="cm"))

    _section_title(pdf, "Phase 2 - Remplissage")
    _kv_row(pdf, "Volume total", _fmt_num(r.get("volume_total_l"), unit="L"))
    _kv_row(pdf, "Hauteur", _fmt_num(r.get("hauteur_cm"), unit="cm"))


# ─── Conditionnement (prévu + réel) ────────────────────────────────────────

def _section_conditionnement(
    pdf, sheet, *, key: str, title: str, with_sscc_meta: bool = False
) -> None:
    section = (sheet.data or {}).get(key) or {}
    items = section.get("items") or []

    _section_title(pdf, title)

    if with_sscc_meta and section.get("sourced_from_sscc"):
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(*_COLOR_GREEN)
        lot_used = section.get("lot_used") or "-"
        fetched = section.get("fetched_at") or "-"
        pdf.cell(0, 5, _latin1(
            f"Source SSCC | Lot {lot_used} | Fetched at {fetched[:19]}",
        ), ln=True)
        pdf.set_text_color(*_COLOR_INK)

    if not items:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*_COLOR_MUTED)
        pdf.cell(0, 5, _latin1("Aucune ligne."), ln=True)
        return

    # Conditionnement réel : colonnes supplémentaires Echantillons + Tracabilite
    # (saisies par produit dans la vue iOS unifiée — refonte 2026-05).
    # Pour le prévisionnel ces champs n'existent pas.
    if with_sscc_meta:
        col_widths = [20, 22, 36, 20, 16, 22, 22]
        headers = ["Format", "Marque", "Designation", "Cartons", "Palettes",
                   "Echant.", "Tracab."]
    else:
        col_widths = [30, 30, 60, 25, 25]
        headers = ["Format", "Marque", "Designation", "Cartons", "Palettes"]

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*_COLOR_MUTED)
    pdf.set_fill_color(*_COLOR_BG_LIGHT)
    for w, h in zip(col_widths, headers):
        pdf.cell(w, 6, _latin1(h), border=1, fill=True, ln=False)
    pdf.ln(6)

    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*_COLOR_INK)
    total_cartons = 0
    total_palettes = 0
    total_echantillons = 0
    total_tracabilite = 0
    for item in items:
        if with_sscc_meta:
            echant = int(item.get("echantillons_cartons") or 0)
            tracab = int(item.get("tracabilite_cartons") or 0)
            row = [
                item.get("fmt") or "-",
                item.get("marque") or "-",
                (item.get("designation") or "")[:35],
                str(int(item.get("cartons") or 0)),
                str(int(item.get("palettes") or 0)),
                str(echant) if echant else "-",
                str(tracab) if tracab else "-",
            ]
            total_echantillons += echant
            total_tracabilite += tracab
        else:
            row = [
                item.get("fmt") or "-",
                item.get("marque") or "-",
                (item.get("designation") or "")[:50],
                str(int(item.get("cartons") or 0)),
                str(int(item.get("palettes") or 0)),
            ]
        for w, val in zip(col_widths, row):
            pdf.cell(w, 6, _latin1(val), border=1, ln=False)
        pdf.ln(6)
        total_cartons += int(item.get("cartons") or 0)
        total_palettes += int(item.get("palettes") or 0)

    # Ligne total
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*_COLOR_BG_LIGHT)
    if with_sscc_meta:
        pdf.cell(sum(col_widths[:3]), 6, _latin1("Total"), border=1, fill=True, ln=False)
        pdf.cell(col_widths[3], 6, _latin1(str(total_cartons)), border=1, fill=True, ln=False)
        pdf.cell(col_widths[4], 6, _latin1(str(total_palettes)), border=1, fill=True, ln=False)
        pdf.cell(col_widths[5], 6, _latin1(str(total_echantillons) if total_echantillons else "-"),
                 border=1, fill=True, ln=False)
        pdf.cell(col_widths[6], 6, _latin1(str(total_tracabilite) if total_tracabilite else "-"),
                 border=1, fill=True, ln=True)
    else:
        pdf.cell(sum(col_widths[:3]), 6, _latin1("Total"), border=1, fill=True, ln=False)
        pdf.cell(col_widths[3], 6, _latin1(str(total_cartons)), border=1, fill=True, ln=False)
        pdf.cell(col_widths[4], 6, _latin1(str(total_palettes)), border=1, fill=True, ln=True)


# ─── Répartition ───────────────────────────────────────────────────────────

def _section_repartition(pdf, sheet) -> None:
    """Répartition des cartons.

    Source de vérité (post-refonte 2026-05) : champs par item dans
    ``data.conditionnement_reel.items[].echantillons_cartons`` /
    ``tracabilite_cartons``.

    Fallback legacy : ``data.repartition.{echantillons,tracabilite}_cartons``
    (fiches antérieures à la refonte, jamais migrées automatiquement).

    ``antoine_cartons`` reste dans le legacy mais n'est plus saisi via l'UI ;
    on l'affiche s'il est non nul (fiches historiques).
    """
    _section_title(pdf, "Repartition des cartons")

    # Aggrège depuis les items conditionnement_reel (nouveau modèle)
    reel = (sheet.data or {}).get("conditionnement_reel") or {}
    items = reel.get("items") or []
    echantillons = sum(int(it.get("echantillons_cartons") or 0) for it in items)
    tracabilite = sum(int(it.get("tracabilite_cartons") or 0) for it in items)

    # Fallback legacy si les items ne portent pas ces champs
    r = (sheet.data or {}).get("repartition") or {}
    if echantillons == 0 and r.get("echantillons_cartons"):
        echantillons = int(r.get("echantillons_cartons") or 0)
    if tracabilite == 0 and r.get("tracabilite_cartons"):
        tracabilite = int(r.get("tracabilite_cartons") or 0)

    _kv_row(pdf, "Echantillons", str(echantillons))
    _kv_row(pdf, "Tracabilite", str(tracabilite))

    # Antoine : champ legacy uniquement — affiché s'il est non nul
    antoine = int(r.get("antoine_cartons") or 0)
    if antoine:
        _kv_row(pdf, "Antoine (legacy)", str(antoine))

    # Flags clôture brassin (cochés avant finalisation)
    data = sheet.data or {}
    brassin_termine = data.get("brassin_termine")
    archiver = data.get("archiver")
    if brassin_termine or archiver:
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*_COLOR_INK)
        pdf.cell(0, 5, _latin1("Cloture EasyBeer"), ln=True)
        pdf.set_font("Helvetica", "", 9)
        if brassin_termine:
            pdf.set_text_color(*_COLOR_GREEN)
            pdf.cell(0, 5, _latin1("Brassin termine : OUI"), ln=True)
        if archiver:
            pdf.set_text_color(*_COLOR_GREEN)
            pdf.cell(0, 5, _latin1("Brassin archive : OUI"), ln=True)
        pdf.set_text_color(*_COLOR_INK)


# ─── Remarques ─────────────────────────────────────────────────────────────

def _section_remarques(pdf, sheet) -> None:
    _section_title(pdf, "Remarques")
    notes = (sheet.data or {}).get("remarques") or ""
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_COLOR_INK)
    if notes.strip():
        pdf.multi_cell(0, 5, _latin1(notes))
    else:
        pdf.set_text_color(*_COLOR_MUTED)
        pdf.cell(0, 5, _latin1("(Aucune remarque)"), ln=True)


# ─── Incidents (notes + photos) ────────────────────────────────────────────

def _section_incidents(pdf, sheet) -> None:
    _section_title(pdf, "Incidents")
    section = (sheet.data or {}).get("incidents") or {}
    notes = section.get("notes") or ""
    photos = section.get("photos") or []

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_COLOR_INK)
    if notes.strip():
        pdf.multi_cell(0, 5, _latin1(notes))
        pdf.ln(2)
    elif not photos:
        pdf.set_text_color(*_COLOR_MUTED)
        pdf.cell(0, 5, _latin1("(Aucun incident signale)"), ln=True)
        return

    if not photos:
        return

    # Photos : 3 par ligne, chacune 55mm de large
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*_COLOR_INK)
    pdf.cell(0, 5, _latin1(f"Photos ({len(photos)})"), ln=True)
    pdf.ln(1)

    photo_w = 55
    photo_h = 41  # 4:3 ratio
    gap = 4
    start_x = pdf.l_margin
    x = start_x
    y = pdf.get_y()

    tmp_files: list[Path] = []
    try:
        for idx, photo in enumerate(photos):
            img_bytes = _load_photo_bytes(photo)
            if img_bytes is None:
                continue
            # Écriture en fichier temporaire pour FPDF (qui veut un path)
            suffix = ".jpg"
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(img_bytes)
            tmp.close()
            tmp_path = Path(tmp.name)
            tmp_files.append(tmp_path)

            # Saut de page si nécessaire
            if y + photo_h > pdf.h - pdf.b_margin:
                pdf.add_page()
                y = pdf.get_y()
                x = start_x

            try:
                pdf.image(str(tmp_path), x=x, y=y, w=photo_w, h=photo_h)
            except Exception as exc:
                _log.warning("Image incident ignored: %s", exc)

            # Avance horizontal puis retour ligne tous les 3
            if (idx + 1) % 3 == 0:
                x = start_x
                y += photo_h + gap
                pdf.set_y(y)
            else:
                x += photo_w + gap

        # Replace cursor au bas de la dernière ligne de photos
        if len(photos) % 3 != 0:
            pdf.set_y(y + photo_h + gap)

    finally:
        for tmp_path in tmp_files:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


# ─── Chargement des bytes d'une photo (legacy base64 OU OVH S3 via key) ────


def _load_photo_bytes(photo: dict) -> bytes | None:
    """Charge les bytes d'une photo d'incident.

    Deux formats supportés (compatibilité durant la migration Phase B) :

    1. **OVH S3 (key)** — format post-migration :
       Photo a un champ ``key`` (chemin S3). On télécharge via une URL
       signée puis on récupère les bytes via HTTP.

    2. **Base64 (legacy)** — format pré-migration :
       Photo a un champ ``base64``. On décode directement.

    Priorité à ``key`` (S3) car post-migration la source de vérité est S3.
    Retourne ``None`` si aucun des deux formats n'est exploitable.
    """
    if not isinstance(photo, dict):
        return None

    # 1. Priorité S3
    key = (photo.get("key") or "").strip()
    if key:
        try:
            from common.object_storage import OVHStorageError, get_presigned_url

            try:
                url = get_presigned_url(key, ttl_seconds=300)
            except OVHStorageError as exc:
                _log.warning(
                    "PDF photo: get_presigned_url failed (key=%s) : %s",
                    key[-30:], exc,
                )
                return _fallback_base64(photo)

            import requests
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200 and resp.content:
                return resp.content
            _log.warning(
                "PDF photo: HTTP %d en téléchargement (key=%s)",
                resp.status_code, key[-30:],
            )
            return _fallback_base64(photo)
        except Exception as exc:  # noqa: BLE001
            _log.warning("PDF photo: S3 download failed (key=%s) : %s", key[-30:], exc)
            return _fallback_base64(photo)

    # 2. Fallback legacy base64
    return _fallback_base64(photo)


def _fallback_base64(photo: dict) -> bytes | None:
    """Décodage de la photo depuis le champ base64 (legacy). None si absent/invalide."""
    b64 = photo.get("base64") or ""
    if not b64:
        return None
    try:
        # Supporte les data-URLs (data:image/jpeg;base64,...)
        if "," in b64 and b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]
        return base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None
