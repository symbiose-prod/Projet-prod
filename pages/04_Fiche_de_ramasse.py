# pages/04_Fiche_de_ramasse.py
from __future__ import annotations
from common.session import require_login, user_menu, user_menu_footer
user = require_login()  # stoppe la page si non connecté
user_menu()             # nav custom (le bouton logout est dans le footer)

import os, re, datetime as dt, unicodedata, base64
import pandas as pd
import streamlit as st
from dateutil.tz import gettz
from pathlib import Path

from common.design import apply_theme, section, kpi
from common.xlsx_fill import build_bl_enlevements_pdf
from common.email import send_html_with_pdf, _get
from common.easybeer import (
    is_configured as _eb_configured,
    get_brassins_en_cours,
    get_brassin_detail,
    get_planification_matrice,
    get_warehouses,
    get_code_barre_matrice,
)


# ================================ Utilitaires ===============================

def _today_paris() -> dt.date:
    return dt.datetime.now(gettz("Europe/Paris")).date()

def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def _canon(s: str) -> str:
    s = _strip_accents(str(s or "")).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _norm(s: str) -> str:
    s = str(s or "")
    s = s.replace("\u00a0", " ").replace("×", "x")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def _format_from_stock(stock_txt: str) -> str | None:
    """Détecte 12x33 / 6x75 / 4x75 dans un libellé de Stock ou conditionnement."""
    if not stock_txt:
        return None
    s = str(stock_txt).lower().replace("×", "x").replace("\u00a0", " ")

    vol = None
    if "0.33" in s or re.search(r"33\s*c?l", s): vol = 33
    elif "0.75" in s or re.search(r"75\s*c?l", s): vol = 75

    nb = None
    m = re.search(r"(?:carton|pack)\s*de\s*(12|6|4)\b", s)
    if not m: m = re.search(r"\b(12|6|4)\b", s)
    if m: nb = int(m.group(1))

    if vol == 33 and nb == 12: return "12x33"
    if vol == 75 and nb == 6:  return "6x75"
    if vol == 75 and nb == 4:  return "4x75"
    return None


def _clean_product_label(raw_label: str) -> str:
    """
    Nettoie le libellé produit EasyBeer : supprime le suffixe degré (ex. '- 0.0°').
    'Kéfir Pêche - 0.0°' → 'Kéfir Pêche'
    """
    label = str(raw_label or "").strip()
    # Supprimer suffixe degré : "- 0.0°", "- 5.0°", etc.
    label = re.sub(r"\s*-\s*\d+[\.,]?\d*\s*°\s*$", "", label).strip()
    return label


def _extract_gout_from_product(product_label: str) -> str:
    """
    Extrait le goût depuis le libellé produit EasyBeer.
    'Kéfir Gingembre'                     → 'Gingembre'
    'Kéfir de fruits Original'            → 'Original'
    'Infusion probiotique Menthe Poivrée' → 'Menthe Poivrée'
    """
    label = _clean_product_label(product_label)
    for prefix in [
        "Infusion de Kéfir de fruits",
        "Infusion de Kéfir",
        "Infusion probiotique",
        "Kéfir de fruits",
        "Kéfir",
    ]:
        if label.lower().startswith(prefix.lower()):
            return label[len(prefix):].strip()
    return label


# ================================ Catalogue CSV (ref + poids) ===============

INFO_CSV_PATH = "info_FDR.csv"

@st.cache_data(show_spinner=False)
def _load_catalog(path: str) -> pd.DataFrame:
    """Lit info_FDR.csv — utilisé uniquement pour code-barre et poids carton."""
    if not os.path.exists(path):
        return pd.DataFrame(columns=["Produit","Format","Désignation","Code-barre","Poids"])

    df = pd.read_csv(path, encoding="utf-8")
    for c in ["Produit","Format","Désignation","Code-barre"]:
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()

    if "Poids" in df.columns:
        df["Poids"] = df["Poids"].astype(str).str.replace(",", ".", regex=False)
        df["Poids"] = pd.to_numeric(df["Poids"], errors="coerce")

    df["_format_norm"] = df.get("Format","").astype(str).str.lower()
    df["_format_norm"] = df["_format_norm"].str.replace("cl","", regex=False).str.replace(" ", "", regex=False)
    df["_canon_prod"] = df.get("Produit","").map(_canon)
    df["_canon_des"]  = df.get("Désignation","").map(lambda s: _canon(re.sub(r"\(.*?\)", "", s)))
    df["_canon_full"] = (df.get("Produit","").fillna("") + " " + df.get("Désignation","").fillna("")).map(_canon)

    return df


def _csv_lookup(catalog: pd.DataFrame, gout_canon: str, fmt_label: str, prod_hint: str | None = None) -> tuple[str, float] | None:
    """
    Retourne (référence_6_chiffres, poids_carton) via format + goût canonisé.
    """
    if catalog is None or catalog.empty or not fmt_label:
        return None

    fmt_norm = fmt_label.lower().replace("cl", "").replace(" ", "")
    g_can = _canon(gout_canon)

    cand = catalog[catalog["_format_norm"] == fmt_norm]
    if cand.empty:
        cand = catalog[catalog["_format_norm"].str.contains(fmt_norm, na=False)]
    if cand.empty:
        return None

    hint_tokens: list[str] = []
    if prod_hint:
        hint_tokens = [t for t in _canon(prod_hint).split() if t]

    def score_row(row) -> int:
        full = str(row.get("_canon_full") or "")
        prod = str(row.get("_canon_prod") or "")
        score = 0
        if prod == g_can:
            score += 100
        elif g_can and (g_can in prod or prod in g_can):
            score += 70
        if hint_tokens:
            if all(tok in full for tok in hint_tokens):
                score += 25
            elif any(tok in full for tok in hint_tokens):
                score += 10
        if full.startswith("kefir ") or full.startswith("kefir de fruits"):
            score += 5
        wants_water = "water" in g_can
        if not wants_water and "water kefir" in full:
            score -= 30
        if wants_water and "water kefir" in full:
            score += 10
        if "inter" in full and not wants_water:
            score -= 5
        return score

    cand_scored = cand.copy()
    cand_scored["_sc"] = cand_scored.apply(score_row, axis=1)
    cand_scored = cand_scored.sort_values(by="_sc", ascending=False)

    row = cand_scored.iloc[0]
    code = re.sub(r"\D+", "", str(row.get("Code-barre", "")))
    ref6 = code[-6:] if len(code) >= 6 else code
    poids = float(row.get("Poids") or 0.0)

    return (ref6, poids) if ref6 else None


# ================================ EasyBeer =================================

@st.cache_data(ttl=120, show_spinner="Chargement des brassins EasyBeer…")
def _fetch_brassins_en_cours():
    return get_brassins_en_cours()


@st.cache_data(ttl=300, show_spinner="Chargement des codes-barres…")
def _fetch_code_barre_matrice() -> dict[int, list[dict]]:
    """
    Charge la matrice codes-barres et retourne un index par produit :
      idProduit → [{ "ref6": "427014", "fmt_str": "12x33" }, ...]

    Chaque code-barre EasyBeer = un produit/format qui existe réellement.
    """
    data = get_code_barre_matrice()
    by_product: dict[int, list[dict]] = {}
    for prod_entry in data.get("produits", []):
        for cb in prod_entry.get("codesBarres", []):
            code_raw = str(cb.get("code") or "")
            id_produit = (cb.get("modeleProduit") or {}).get("idProduit")
            mod_cont = cb.get("modeleContenant") or {}
            contenance = round(float(mod_cont.get("contenance") or 0), 2)
            mod_lot = cb.get("modeleLot") or {}
            lot_libelle = (mod_lot.get("libelle") or "").strip()

            if not (id_produit and code_raw and contenance):
                continue

            digits = re.sub(r"\D+", "", code_raw)
            ref6 = digits[-6:] if len(digits) >= 6 else digits
            if not ref6:
                continue

            # Dériver le format depuis contenance + lot
            vol_cl = int(contenance * 100)  # 0.33 → 33, 0.75 → 75
            m_pkg = re.search(r"(\d+)", lot_libelle)
            pkg_count = int(m_pkg.group(1)) if m_pkg else 0
            if not (vol_cl and pkg_count):
                continue
            fmt_str = f"{pkg_count}x{vol_cl}"

            by_product.setdefault(id_produit, []).append({
                "ref6": ref6,
                "fmt_str": fmt_str,
            })
    return by_product


def _build_lines_from_brassins(
    selected_brassins: list[dict],
    catalog: pd.DataFrame,
    id_entrepot: int | None,
    cb_by_product: dict[int, list[dict]] | None = None,
) -> tuple[list[dict], dict]:
    """
    Pour chaque brassin sélectionné :
      1. Charge la matrice EasyBeer pour connaître les produits dérivés
      2. Pour chaque produit (principal + dérivés), interroge la matrice
         codes-barres pour savoir quels formats existent réellement

    Retourne (rows, meta_by_label) prêts pour le DataFrame.
    """
    rows: list[dict] = []
    meta_by_label: dict = {}
    seen: set[str] = set()

    for brassin_summary in selected_brassins:
        id_brassin = brassin_summary.get("idBrassin")
        if not id_brassin:
            continue

        brassin_produit = brassin_summary.get("produit") or {}

        # --- Détail du brassin (pour DDM et quantités existantes) ---
        try:
            detail = get_brassin_detail(id_brassin)
        except Exception:
            detail = brassin_summary

        # DDM : depuis les productions existantes ou date début + 365j
        ddm_date = _today_paris() + dt.timedelta(days=365)
        _existing_prods = detail.get("productions") or detail.get("planificationsProductions") or []
        if _existing_prods:
            ddm_str = (
                _existing_prods[0].get("dateLimiteUtilisationOptimaleFormulaire")
                or _existing_prods[0].get("dateLimiteUtilisationOptimale")
                or ""
            )
            if ddm_str:
                try:
                    ddm_date = dt.date.fromisoformat(ddm_str[:10])
                except (ValueError, TypeError):
                    pass
        else:
            date_debut_str = detail.get("dateDebutFormulaire") or ""
            if date_debut_str:
                try:
                    ddm_date = dt.date.fromisoformat(date_debut_str[:10]) + dt.timedelta(days=365)
                except (ValueError, TypeError):
                    pass

        # Index des quantités existantes : (prod_libelle_lower, fmt_str) → quantité
        _existing_qty: dict[tuple[str, str], int] = {}
        for _pe in _existing_prods:
            _pe_label = ((_pe.get("produit") or {}).get("libelle") or "").lower()
            _pe_cond = str(_pe.get("conditionnement") or "")
            _pe_fmt = _format_from_stock(_pe_cond) or _format_from_stock(_pe_label)
            _pe_qty = int(_pe.get("quantite") or 0)
            if _pe_label and _pe_fmt:
                _existing_qty[(_pe_label, _pe_fmt)] = _pe_qty

        # --- Matrice EasyBeer : uniquement pour les produits dérivés ---
        produits_derives: list[dict] = []
        if id_entrepot:
            try:
                matrice = get_planification_matrice(id_brassin, id_entrepot)
                produits_derives = matrice.get("produitsDerives", [])
            except Exception:
                pass

        # --- Tous les produits : principal + dérivés ---
        all_products: list[dict] = [brassin_produit]
        for pd_item in produits_derives:
            if pd_item.get("libelle"):
                all_products.append(pd_item)

        # --- Générer les lignes depuis la matrice codes-barres ---
        # Chaque code-barre EasyBeer = un produit/format qui existe réellement
        for prod in all_products:
            prod_label = (prod.get("libelle") or "").strip()
            id_produit = prod.get("idProduit")
            if not prod_label or not id_produit:
                continue

            clean_label = _clean_product_label(prod_label)
            gout = _extract_gout_from_product(prod_label)

            # Formats existants depuis la matrice codes-barres
            formats = (cb_by_product or {}).get(id_produit, [])

            for pf in formats:
                ref = pf["ref6"]
                fmt_str = pf["fmt_str"]

                label = f"{clean_label} — {fmt_str}cl"
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)

                # Poids carton depuis le CSV
                poids_carton = 0.0
                lk = _csv_lookup(catalog, gout, fmt_str, prod_label)
                if lk:
                    poids_carton = lk[1]

                # Quantité pré-remplie depuis productions existantes
                qty = _existing_qty.get((prod_label.lower(), fmt_str), 0)

                meta_by_label[label] = {
                    "_format": fmt_str,
                    "_poids_carton": poids_carton,
                    "_reference": ref,
                }
                rows.append({
                    "Référence": ref,
                    "Produit (goût + format)": label,
                    "DDM": ddm_date,
                    "Quantité cartons": qty,
                    "Quantité palettes": 0,
                    "Poids palettes (kg)": 0,
                })

    return rows, meta_by_label


# ================================ Email =====================================

def _inline_img_from_repo(rel_path: str) -> str:
    p = Path(rel_path)
    if not p.exists():
        return ""
    try:
        data = p.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        return f'<img src="data:image/png;base64,{b64}" style="height:40px;margin-right:8px;" alt="">'
    except Exception:
        return ""

def send_mail_with_pdf(
    pdf_bytes: bytes,
    filename: str,
    total_palettes: int,
    to_list: list[str],
    date_ramasse: dt.date,
    bcc_me: bool = True,
):
    subject = f"Demande de ramasse — {date_ramasse:%d/%m/%Y} — Ferment Station"

    body_html = f"""
    <p>Bonjour,</p>
    <p>Nous aurions besoin d'une ramasse pour demain.<br>
    Pour <strong>{total_palettes}</strong> palette{'s' if total_palettes != 1 else ''}.</p>
    <p>Merci,<br>Bon après-midi.</p>
    """

    logo_symbiose = _inline_img_from_repo("assets/signature/logo_symbiose.png")
    logo_niko     = _inline_img_from_repo("assets/signature/NIKO_Logo.png")

    signature_html = f"""
    <hr>
    <p style="font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
        <strong>Ferment Station</strong><br>
        Producteur de boissons fermentées<br>
        26 Rue Robert Witchitz – 94200 Ivry-sur-Seine<br>
        <a href="tel:+33971227895">09 71 22 78 95</a>
    </p>
    <p>{logo_symbiose}{logo_niko}</p>
    """

    html = body_html + signature_html

    sender = _get("EMAIL_SENDER")
    recipients = list(to_list)
    if bcc_me and sender and sender not in recipients:
        recipients.append(sender)

    for rcpt in recipients:
        send_html_with_pdf(
            to_email=rcpt,
            subject=subject,
            html_body=html,
            attachments=[(filename, pdf_bytes)],
        )


# ================================ Réglages ==================================

DEST_TITLE = "SOFRIPA"
DEST_LINES = [
    "ZAC du Haut de Wissous II,",
    "Rue Hélène Boucher, 91320 Wissous",
]

DEFAULT_RECIPIENTS_FALLBACK = "z.dawam@sofripa.fr, nicolas@symbiose-kefir.fr, g.marlier@sofripa.fr, f.ricard@sofripa.fr, c.boulon@sofripa.fr, a.teixeira@sofripa.fr, prepa@sofripa.fr, annonces@sofripa.fr, exploitation@sofripa.fr, b.alves@sofripa.fr"


# ================================== UI ======================================

apply_theme("Fiche de ramasse — Ferment Station", "🚚")
section("Fiche de ramasse", "🚚")

# Garde : EasyBeer doit être configuré
if not _eb_configured():
    st.warning("EasyBeer n'est pas configuré (variables EASYBEER_API_USER / EASYBEER_API_PASS manquantes).")
    st.stop()

# Charger le catalogue CSV (fallback poids carton)
catalog = _load_catalog(INFO_CSV_PATH)

# Charger les codes-barres EasyBeer (indexés par idProduit)
_cb_by_product: dict[int, list[dict]] | None = None
try:
    _cb_by_product = _fetch_code_barre_matrice()
except Exception:
    pass

# Charger les brassins en cours
try:
    _all_brassins = _fetch_brassins_en_cours()
except Exception as e:
    st.error(f"Erreur de connexion à EasyBeer : {e}")
    _all_brassins = []

# Entrepôt principal (nécessaire pour la matrice)
_id_entrepot: int | None = None
try:
    _warehouses = get_warehouses()
    for _w in _warehouses:
        if _w.get("principal"):
            _id_entrepot = _w.get("idEntrepot")
            break
    if not _id_entrepot and _warehouses:
        _id_entrepot = _warehouses[0].get("idEntrepot")
except Exception:
    pass

_brassins_valides = [b for b in _all_brassins if not b.get("annule")]

if not _brassins_valides:
    st.info("Aucun brassin en cours dans EasyBeer.")
    st.stop()

# Labels pour le multiselect
def _brassin_label(b: dict) -> str:
    nom = b.get("nom", "?")
    prod = _clean_product_label((b.get("produit") or {}).get("libelle", "?"))
    vol = b.get("volume", 0)
    return f"{nom} — {prod} — {vol:.0f}L"

_brassin_labels = [_brassin_label(b) for b in _brassins_valides]

st.subheader("Sélection des brassins")
selected_labels = st.multiselect(
    "Brassins à inclure",
    options=_brassin_labels,
    default=[],
    key="ramasse_eb_brassins",
)

_selected_brassins = [
    _brassins_valides[_brassin_labels.index(lbl)]
    for lbl in selected_labels
    if lbl in _brassin_labels
]

meta_by_label: dict = {}
rows: list[dict] = []

if _selected_brassins:
    with st.spinner("Chargement des détails brassins…"):
        rows, meta_by_label = _build_lines_from_brassins(_selected_brassins, catalog, _id_entrepot, _cb_by_product)
else:
    st.info("Sélectionne au moins un brassin pour construire la fiche.")

# Sidebar
with st.sidebar:
    st.header("Paramètres")
    date_creation = _today_paris()
    date_ramasse = st.date_input("Date de ramasse", value=date_creation)

    if st.button("🔄 Recharger", use_container_width=True):
        _load_catalog.clear()
        _fetch_brassins_en_cours.clear()
        _fetch_code_barre_matrice.clear()
        st.rerun()

    st.caption(f"DATE DE CRÉATION : **{date_creation.strftime('%d/%m/%Y')}**")
    st.markdown("---")
    user_menu_footer(user)

# Table éditable
display_cols = ["Référence", "Produit (goût + format)", "DDM", "Quantité cartons", "Quantité palettes", "Poids palettes (kg)"]
base_df = pd.DataFrame(rows, columns=display_cols) if rows else pd.DataFrame(columns=display_cols)

if not base_df.empty:
    st.caption("Renseigne **Quantité cartons** et **Quantité palettes**. Le **poids** se calcule automatiquement.")
    edited = st.data_editor(
        base_df,
        key="ramasse_editor_v2",
        use_container_width=True,
        hide_index=True,
        column_config={
            "DDM": st.column_config.DateColumn(label="DDM", format="DD/MM/YYYY"),
            "Quantité cartons": st.column_config.NumberColumn(min_value=0, step=1),
            "Quantité palettes": st.column_config.NumberColumn(min_value=0, step=1),
            "Poids palettes (kg)": st.column_config.NumberColumn(disabled=True, format="%.0f"),
        },
    )
else:
    edited = base_df

# Calculs poids
def _apply_calculs(df_disp: pd.DataFrame) -> pd.DataFrame:
    out = df_disp.copy()
    poids = []
    for _, r in out.iterrows():
        lab = str(r["Produit (goût + format)"]).replace(" - ", " — ")
        meta = meta_by_label.get(lab, meta_by_label.get(str(r["Produit (goût + format)"]), {}))
        pc = float(meta.get("_poids_carton", 0.0))
        cartons = int(pd.to_numeric(r["Quantité cartons"], errors="coerce") or 0)
        poids.append(int(round(cartons * pc, 0)))
    out["Poids palettes (kg)"] = poids
    return out

df_calc = _apply_calculs(edited)

# KPIs
tot_cartons  = int(pd.to_numeric(df_calc["Quantité cartons"], errors="coerce").fillna(0).sum())
tot_palettes = int(pd.to_numeric(df_calc["Quantité palettes"], errors="coerce").fillna(0).sum())
tot_poids    = int(pd.to_numeric(df_calc["Poids palettes (kg)"], errors="coerce").fillna(0).sum())

c1, c2, c3 = st.columns(3)
with c1: kpi("Total cartons", f"{tot_cartons:,}".replace(",", " "))
with c2: kpi("Total palettes", f"{tot_palettes}")
with c3: kpi("Poids total (kg)", f"{tot_poids:,}".replace(",", " "))
st.dataframe(df_calc[display_cols], use_container_width=True, hide_index=True)

# Téléchargement PDF
if st.button("🧾 Télécharger la version PDF", use_container_width=True):
    if tot_cartons <= 0:
        st.error("Renseigne au moins une **Quantité cartons** > 0.")
    else:
        try:
            df_for_export = df_calc[display_cols].copy()
            if not pd.api.types.is_string_dtype(df_for_export["DDM"]):
                df_for_export["DDM"] = df_for_export["DDM"].apply(
                    lambda d: d.strftime("%d/%m/%Y") if hasattr(d, "strftime") else str(d)
                )

            pdf_bytes = build_bl_enlevements_pdf(
                date_creation=_today_paris(),
                date_ramasse=date_ramasse,
                destinataire_title=DEST_TITLE,
                destinataire_lines=DEST_LINES,
                df_lines=df_for_export,
            )
            st.session_state["fiche_ramasse_pdf"] = pdf_bytes
            st.download_button(
                "📄 Télécharger la version PDF",
                data=pdf_bytes,
                file_name=f"Fiche_de_ramasse_{date_ramasse:%Y%m%d}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as e:
            st.error(f"Erreur PDF : {e}")

# ======================== ENVOI PAR E-MAIL ====================================

total_palettes = tot_palettes  # alias pour l'email
pdf_bytes = st.session_state.get("fiche_ramasse_pdf")

sender_hint = os.environ.get("EMAIL_SENDER") or os.environ.get("EMAIL_USER")

if "ramasse_email_to" not in st.session_state or not st.session_state["ramasse_email_to"].strip():
    st.session_state["ramasse_email_to"] = DEFAULT_RECIPIENTS_FALLBACK

to_input = st.text_input(
    "Destinataires (séparés par des virgules)",
    key="ramasse_email_to",
)

def _parse_emails(s: str) -> list[str]:
    return [e.strip() for e in (s or "").split(",") if e.strip()]

to_list = _parse_emails(st.session_state.get("ramasse_email_to", ""))

if sender_hint:
    st.caption(f"Expéditeur utilisé : **{sender_hint}**")

if st.button("✉️ Envoyer la demande de ramasse", type="primary", use_container_width=True):
    if pdf_bytes is None:
        if tot_cartons <= 0:
            st.error("Le PDF n'est pas prêt et aucun carton n'est saisi. Renseigne au moins une quantité > 0 puis clique à nouveau.")
            st.stop()
        try:
            df_for_export = df_calc[display_cols].copy()
            if not pd.api.types.is_string_dtype(df_for_export["DDM"]):
                df_for_export["DDM"] = df_for_export["DDM"].apply(
                    lambda d: d.strftime("%d/%m/%Y") if hasattr(d, "strftime") else str(d)
                )
            pdf_bytes = build_bl_enlevements_pdf(
                date_creation=_today_paris(),
                date_ramasse=date_ramasse,
                destinataire_title=DEST_TITLE,
                destinataire_lines=DEST_LINES,
                df_lines=df_for_export,
            )
            st.session_state["fiche_ramasse_pdf"] = pdf_bytes
        except Exception as e:
            st.error(f"Erreur PDF : {e}")
            st.stop()

    if not to_list:
        st.error("Indique au moins un destinataire.")
    else:
        try:
            filename = f"Fiche_de_ramasse_{date_ramasse.strftime('%Y%m%d')}.pdf"
            size_kb = len(pdf_bytes) / 1024
            st.caption(f"Taille PDF : {size_kb:.0f} Ko")

            send_mail_with_pdf(
                pdf_bytes=pdf_bytes,
                filename=filename,
                total_palettes=tot_palettes,
                to_list=to_list,
                date_ramasse=date_ramasse,
                bcc_me=True
            )

            st.success(f"📨 Demande de ramasse envoyée à {len(to_list)} destinataire(s).")
        except Exception as e:
            st.error(f"Échec de l'envoi : {e}")
