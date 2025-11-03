# pages/04_Fiche_de_ramasse.py
from __future__ import annotations
from common.session import require_login, user_menu, user_menu_footer
user = require_login()  # stoppe la page si non connecté
user_menu()             # nav custom (le bouton logout est dans le footer)

import os, re, datetime as dt, unicodedata
import pandas as pd
import streamlit as st
from dateutil.tz import gettz

from common.design import apply_theme, section, kpi
import importlib
import common.xlsx_fill as _xlsx_fill
importlib.reload(_xlsx_fill)
from common.xlsx_fill import fill_bl_enlevements_xlsx, build_bl_enlevements_pdf
from common.email import send_html_with_pdf, html_signature, _get_ns, _get
from common.storage import list_saved, load_snapshot
from pathlib import Path


# ================================ Normalisation ===============================

def _norm(s: str) -> str:
    # normalise unicode + nettoie espaces/insécables + remplace le signe '×' par 'x'
    s = str(s or "")
    s = s.replace("\u00a0", " ").replace("×", "x")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def _build_opts_from_catalog(catalog: pd.DataFrame) -> pd.DataFrame:
    """
    Construit la liste de TOUS les produits du CSV (manuel), sans dédup agressive,
    en normalisant Produit/Format pour éviter les caractères piégeux.
    """
    if catalog is None or catalog.empty:
        return pd.DataFrame(columns=["label","gout","format","prod_hint"])

    rows = []
    for _, r in catalog.iterrows():
        gout = _norm(r.get("Produit", ""))
        fmt  = _norm(r.get("Format", ""))
        des  = _norm(r.get("Désignation", ""))
        if not (gout and fmt):
            continue
        rows.append({
            "label": f"{gout} — {fmt}",
            "gout": gout,
            "format": fmt,
            "prod_hint": des,
        })
    return pd.DataFrame(rows).sort_values(by="label").reset_index(drop=True)


# ================================== EMAIL (wrapper) ===========================

def _default_recipients_from_cfg() -> list[str]:
    """
    Lit d'abord EMAIL_RECIPIENTS (env), fallback st.secrets['email']['recipients'] (string ou liste).
    """
    cfg = _get_ns("email", "recipients") or _get("EMAIL_RECIPIENTS", "")
    if isinstance(cfg, list):
        return [x.strip() for x in cfg if x and str(x).strip()]
    if isinstance(cfg, str):
        return [x.strip() for x in cfg.split(",") if x.strip()]
    return []

def send_mail_with_pdf(
    pdf_bytes: bytes,
    filename: str,
    total_palettes: int,
    to_list: list[str],
    date_ramasse: dt.date,
    bcc_me: bool = True
):
    """
    Envoi via common.email → API Brevo (ou autre backend selon env).
    - Corps HTML + signature inline
    - PDF en pièce jointe
    """
    subject = f"Demande de ramasse — {date_ramasse:%d/%m/%Y} — Ferment Station"

    body_html = f"""
    <p>Bonjour,</p>
    <p>Nous aurions besoin d’une ramasse pour demain.<br>
    Pour <strong>{total_palettes}</strong> palettes.</p>
    <p>Merci,<br>Bon après-midi.</p>
    """
    # On compose : corps + signature
    html = body_html + html_signature()

    # BCC expéditeur si demandé (on l’obtient via EMAIL_SENDER / [email].sender)
    sender = _get_ns("email", "sender") or _get("EMAIL_SENDER")
    recipients = list(to_list)
    if bcc_me and sender:
        if sender not in recipients:
            recipients.append(sender)

    # Envoi (exceptions remontent pour affichage UI)
    send_html_with_pdf(
        subject=subject,
        html_body=html,
        recipients=recipients,
        pdf_bytes=pdf_bytes,
        pdf_name=filename
    )


# ================================ Réglages ====================================

INFO_CSV_PATH = "info_FDR.csv"
TEMPLATE_XLSX_PATH = "assets/BL_enlevements_Sofripa.xlsx"

DEST_TITLE = "SOFRIPA"
DEST_LINES = [
    "ZAC du Haut de Wissous II,",
    "Rue Hélène Boucher, 91320 Wissous",
]

# ================================ Utils =======================================

def _today_paris() -> dt.date:
    return dt.datetime.now(gettz("Europe/Paris")).date()

def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def _canon(s: str) -> str:
    s = _strip_accents(str(s or "")).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _format_from_stock(stock_txt: str) -> str | None:
    """
    Détecte 12x33 / 6x75 / 4x75 dans un libellé de Stock.
    """
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

@st.cache_data(show_spinner=False)
def _load_catalog(path: str) -> pd.DataFrame:
    """
    Lit info_FDR.csv et prépare colonnes auxiliaires pour le matching.
    """
    if not os.path.exists(path):
        return pd.DataFrame(columns=["Produit","Format","Désignation","Code-barre","Poids"])

    df = pd.read_csv(path, encoding="utf-8")
    for c in ["Produit","Format","Désignation","Code-barre"]:
        if c in df.columns:
            df[c] = df[c].astype(str).str.strip()

    if "Poids" in df.columns:
        df["Poids"] = (
            df["Poids"].astype(str).str.replace(",", ".", regex=False)
        )
        df["Poids"] = pd.to_numeric(df["Poids"], errors="coerce")

    df["_format_norm"] = df.get("Format","").astype(str).str.lower()
    df["_format_norm"] = df["_format_norm"].str.replace("cl","", regex=False).str.replace(" ", "", regex=False)

    df["_canon_prod"] = df.get("Produit","").map(_canon)
    df["_canon_des"]  = df.get("Désignation","").map(lambda s: _canon(re.sub(r"\(.*?\)", "", s)))
    df["_canon_full"] = (df.get("Produit","").fillna("") + " " + df.get("Désignation","").fillna("")).map(_canon)

    return df

def _csv_lookup(catalog: pd.DataFrame, gout_canon: str, fmt_label: str, prod_hint: str | None = None) -> tuple[str, float] | None:
    """
    Retourne (référence_6_chiffres, poids_carton) via :
      - format (12x33 / 6x75 / 4x75)
      - + goût canonisé
      - + (optionnel) 'prod_hint' pour privilégier une marque/ligne précise (ex. NIKO)
    """
    if catalog is None or catalog.empty or not fmt_label:
        return None

    fmt_norm = fmt_label.lower().replace("cl","").replace(" ", "")
    g_can = _canon(gout_canon)

    cand = catalog[catalog["_format_norm"].str.contains(fmt_norm, na=False)]
    if cand.empty:
        return None

    hint_tokens = []
    if prod_hint:
        hint_tokens = [t for t in _canon(prod_hint).split() if t]

    def score_row(row) -> tuple[int, int]:
        s1 = 1 if row.get("_canon_prod") == g_can else 0
        full = str(row.get("_canon_full") or "")
        s2 = 1 if (hint_tokens and all(tok in full for tok in hint_tokens)) else 0
        s3 = 1 if (hint_tokens and any(tok in full for tok in hint_tokens)) else 0
        return (s1 + s2, s3)

    cand_scored = cand.copy()
    cand_scored["_sc"] = cand_scored.apply(score_row, axis=1)
    cand_scored = cand_scored.sort_values(by="_sc", ascending=False)

    row = cand_scored.iloc[0]
    code = re.sub(r"\D+", "", str(row.get("Code-barre","")))
    ref6 = code[-6:] if len(code) >= 6 else code
    poids = float(row.get("Poids") or 0.0)
    return (ref6, poids) if ref6 else None

def _build_opts_from_saved(df_min_saved: pd.DataFrame) -> pd.DataFrame:
    """
    Construit les options depuis la proposition sauvegardée, en ne gardant
    que les produits dont le nombre de cartons à produire > 0.
    """
    if df_min_saved is None or df_min_saved.empty:
        return pd.DataFrame(columns=["label", "gout", "format", "prod_hint"])

    CAND_QTY_COLS = [
        "Cartons à produire (arrondi)",
        "Cartons à produire",
        "CartonsArrondis",
        "Cartons_produire",
        "Cartons",
    ]
    qty_col = next((c for c in CAND_QTY_COLS if c in df_min_saved.columns), None)

    df_src = df_min_saved.copy()
    if qty_col:
        qty = pd.to_numeric(df_src[qty_col], errors="coerce").fillna(0)
        df_src = df_src[qty > 0]

    if df_src.empty:
        return pd.DataFrame(columns=["label", "gout", "format", "prod_hint"])

    rows, seen = [], set()
    for _, r in df_src.iterrows():
        gout = str(r.get("GoutCanon") or "").strip()
        prod_txt  = _norm(r.get("Produit", ""))
        stock_txt = _norm(r.get("Stock", ""))
        fmt = (
            _format_from_stock(stock_txt)
            or _format_from_stock(_norm(r.get("Format", "")))
            or _format_from_stock(_norm(r.get("Designation", "")))
            or _format_from_stock(prod_txt)
        )
        if not gout or not fmt:
            continue

        label = f"{prod_txt} — {stock_txt}" if prod_txt and stock_txt else f"{gout} — {fmt}"

        key = label.lower()
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "label": label,
            "gout": gout,
            "format": fmt,
            "prod_hint": (prod_txt or label),
        })

    return pd.DataFrame(rows).sort_values(by="label").reset_index(drop=True)


# ================================== UI =======================================

apply_theme("Fiche de ramasse — Ferment Station", "🚚")
section("Fiche de ramasse", "🚚")

# 0) Choix de la source (un seul radio)
source_mode = st.radio(
    "Source des produits pour la fiche",
    options=["Proposition sauvegardée", "Sélection manuelle"],
    horizontal=True,
    key="ramasse_source_mode",
)

# 1) Charger le catalogue (utile en manuel et pour les références/poids)
catalog = _load_catalog(INFO_CSV_PATH)
if catalog.empty:
    st.warning("⚠️ `info_FDR.csv` introuvable ou vide — références/poids non calculables.")

# 2) Construire la liste des produits selon le mode
if source_mode == "Proposition sauvegardée":
    sp = st.session_state.get("saved_production")
    if not sp or "df_min" not in sp:
        st.warning(
            "Va d’abord dans **Production** et clique **💾 Sauvegarder cette production** "
            "ou charge une proposition depuis la mémoire longue ci-dessous."
        )
        saved = list_saved()
        if saved:
            labels = [f"{it['name']} — ({it.get('semaine_du','?')})" for it in saved]
            sel = st.selectbox("Charger une proposition enregistrée", options=labels)
            if st.button("▶️ Charger cette proposition", use_container_width=True):
                picked_name = saved[labels.index(sel)]["name"]
                sp_loaded = load_snapshot(picked_name)
                if sp_loaded and sp_loaded.get("df_min") is not None:
                    st.session_state["saved_production"] = sp_loaded
                    st.success(f"Chargé : {picked_name}")
                    st.rerun()
                else:
                    st.error("Proposition invalide (df_min manquant).")
        st.stop()

    df_min_saved: pd.DataFrame = sp["df_min"].copy()
    ddm_saved = dt.date.fromisoformat(sp["ddm"]) if "ddm" in sp else _today_paris()
    opts_df = _build_opts_from_saved(df_min_saved)

else:  # "Sélection manuelle"
    df_min_saved = None
    ddm_saved = _today_paris()  # valeur par défaut de secours
    opts_df = _build_opts_from_catalog(catalog)

if opts_df.empty:
    st.error("Aucun produit détecté pour ce mode (vérifie `info_FDR.csv`).")
    st.stop()

# 3) Sidebar : dates + actions + footer (doit rester en dernier)
with st.sidebar:
    st.header("Paramètres")
    date_creation = _today_paris()
    date_ramasse = st.date_input("Date de ramasse", value=date_creation)
    if st.button("🔄 Recharger le catalogue", use_container_width=True):
        _load_catalog.clear()
        st.rerun()
    # DDM selon le mode
    if source_mode == "Sélection manuelle":
        ddm_manual = st.date_input("DDM par défaut (manuel)", value=_today_paris())
    st.caption(f"DATE DE CRÉATION : **{date_creation.strftime('%d/%m/%Y')}**")
    if source_mode == "Proposition sauvegardée":
        st.caption(f"DDM (depuis Production) : **{ddm_saved.strftime('%d/%m/%Y')}**")

    # Footer logout tout en bas de la sidebar
    st.markdown("---")
    user_menu_footer()

# 4) Sélection utilisateur
st.subheader("Sélection des produits")
selection_labels = st.multiselect(
    "Produits à inclure (Goût — Format)",
    options=opts_df["label"].tolist(),
    default=opts_df["label"].tolist() if source_mode == "Proposition sauvegardée" else [],
)

# 5) Table éditable
meta_by_label = {}
rows = []
ddm_default = ddm_saved if source_mode == "Proposition sauvegardée" else ddm_manual
for lab in selection_labels:
    row_opt = opts_df.loc[opts_df["label"] == lab].iloc[0]
    gout = row_opt["gout"]
    fmt  = row_opt["format"]
    prod_hint = row_opt.get("prod_hint") or row_opt.get("label")
    ref = ""; poids_carton = 0.0
    lk = _csv_lookup(catalog, gout, fmt, prod_hint)
    if lk: ref, poids_carton = lk
    meta_by_label[lab] = {"_format": fmt, "_poids_carton": poids_carton, "_reference": ref}
    rows.append({
        "Référence": ref,
        "Produit (goût + format)": lab,
        "DDM": ddm_default,
        "Quantité cartons": 0,
        "Quantité palettes": 0,
        "Poids palettes (kg)": 0,
    })
display_cols = ["Référence","Produit (goût + format)","DDM","Quantité cartons","Quantité palettes","Poids palettes (kg)"]
base_df = pd.DataFrame(rows, columns=display_cols)

st.caption("Renseigne **Quantité cartons** et, si besoin, **Quantité palettes**. Le **poids** se calcule automatiquement (cartons × poids/carton du CSV).")
edited = st.data_editor(
    base_df,
    key="ramasse_editor_xlsx_v1",
    use_container_width=True,
    hide_index=True,
    column_config={
        "DDM": st.column_config.DateColumn(
            label="DDM",
            format="DD/MM/YYYY",
            disabled=(source_mode == "Proposition sauvegardée")
        ),
        "Quantité cartons":  st.column_config.NumberColumn(min_value=0, step=1),
        "Quantité palettes": st.column_config.NumberColumn(min_value=0, step=1),
        "Poids palettes (kg)": st.column_config.NumberColumn(disabled=True, format="%.0f"),
    },
)

# 6) Calculs
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

# 7-bis) Téléchargement PDF
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
# 1) Total palettes
PALETTE_COL_CANDIDATES = ["Quantité palettes", "N° palettes", "Nb palettes", "Quantite palettes"]
pal_col = next((c for c in PALETTE_COL_CANDIDATES if c in df_calc.columns), None)
if pal_col is None:
    st.error("Colonne des palettes introuvable dans df_calc. Renomme une des colonnes en " + ", ".join(PALETTE_COL_CANDIDATES))
else:
    total_palettes = int(pd.to_numeric(df_calc[pal_col], errors="coerce").fillna(0).sum())

    # 2) Récup PDF (ou possibilité de régénérer si absent)
    pdf_bytes = st.session_state.get("fiche_ramasse_pdf")

    # 3) UI destinataires (pré-remplie et persistante)
    try:
        sender_hint = _get_ns("email", "sender") or _get("EMAIL_SENDER") or _get_ns("email", "user") or _get("EMAIL_USER")
        rec_list = _default_recipients_from_cfg()
        rec_str = ", ".join(rec_list)
    except Exception:
        sender_hint = None
        rec_str = ""

    if "ramasse_email_to" not in st.session_state:
        st.session_state["ramasse_email_to"] = rec_str or ""

    to_input = st.text_input(
        "Destinataires (séparés par des virgules)",
        key="ramasse_email_to",
        placeholder="ex: logistique@transporteur.com, expeditions@tonentreprise.fr",
    )

    def _parse_emails(s: str):
        return [e.strip() for e in (s or "").split(",") if e.strip()]

    to_list = _parse_emails(st.session_state.get("ramasse_email_to",""))

    if sender_hint:
        st.caption(f"Expéditeur utilisé : **{sender_hint}**")

    # Envoi
    if st.button("✉️ Envoyer la demande de ramasse", type="primary", use_container_width=True):
        if pdf_bytes is None:
            if tot_cartons <= 0:
                st.error("Le PDF n’est pas prêt et aucun carton n’est saisi. Renseigne au moins une quantité > 0 puis clique à nouveau.")
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
                    total_palettes=total_palettes,
                    to_list=to_list,
                    date_ramasse=date_ramasse,
                    bcc_me=True
                )

                st.write("Destinataires envoyés :", ", ".join(to_list))
                st.success("📨 Demande de ramasse envoyée (backend e-mail OK).")
            except Exception as e:
                st.error(f"Échec de l’envoi : {e}")
