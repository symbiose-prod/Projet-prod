# pages/04_Fiche_de_ramasse.py
"""
Fiche de ramasse — Ferment Station
===================================
Genere un BL (Bon de Livraison) PDF a partir des brassins EasyBeer en cours,
calcule les poids, et envoie la demande de ramasse par email (Brevo).
"""
from __future__ import annotations

from common.session import require_login, user_menu, user_menu_footer

user = require_login()
user_menu()

import base64
import datetime as dt
import math
import os

import pandas as pd
import streamlit as st
from pathlib import Path

from common.design import apply_theme, section, kpi
from common.xlsx_fill import build_bl_enlevements_pdf
from common.email import send_html_with_pdf, _get
from common.easybeer import (
    is_configured as _eb_configured,
    get_brassins_en_cours,
    get_brassins_archives,
    get_code_barre_matrice,
    get_warehouses,
    fetch_carton_weights,
)
from common.ramasse import (
    today_paris,
    clean_product_label,
    parse_barcode_matrix,
    build_ramasse_lines,
    get_carton_weight,
    load_destinataires,
    PALETTE_EMPTY_WEIGHT,
)


# ================================ Theme ======================================

apply_theme("Fiche de ramasse — Ferment Station", "\U0001F69A")
section("Fiche de ramasse", "\U0001F69A")

# ================================ Guards =====================================

if not _eb_configured():
    st.warning("EasyBeer n'est pas configuré (variables EASYBEER_API_USER / EASYBEER_API_PASS manquantes).")
    st.stop()

# ================================ Destinataires ==============================

_destinataires = load_destinataires()
_dest_names = [d["name"] for d in _destinataires] if _destinataires else ["SOFRIPA"]

# ================================ Sidebar ====================================

with st.sidebar:
    st.header("Paramètres")

    date_creation = today_paris()
    date_ramasse = st.date_input("Date de ramasse", value=date_creation)

    # Selectbox destinataire
    selected_dest_name = st.selectbox(
        "Destinataire",
        options=_dest_names,
        index=0,
        key="ramasse_destinataire",
    )

    # Lookup destinataire selectionne
    _dest_obj = next((d for d in _destinataires if d["name"] == selected_dest_name), None)
    if _dest_obj:
        dest_title = _dest_obj["name"]
        dest_lines = _dest_obj.get("address_lines", [])
        dest_emails = _dest_obj.get("email_recipients", [])
    else:
        dest_title = selected_dest_name
        dest_lines = []
        dest_emails = []

    if st.button("\U0001F504 Recharger", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.caption(f"DATE DE CRÉATION : **{date_creation.strftime('%d/%m/%Y')}**")
    st.markdown("---")
    user_menu_footer(user)

# ================================ Chargement donnees =========================

# Brassins en cours + 3 derniers archivés
@st.cache_data(ttl=120, show_spinner="Chargement des brassins EasyBeer…")
def _fetch_brassins():
    return get_brassins_en_cours()

@st.cache_data(ttl=120, show_spinner="Chargement des brassins archivés…")
def _fetch_brassins_archives():
    return get_brassins_archives(nombre=3)

try:
    _all_brassins = _fetch_brassins()
except Exception as e:
    st.error(f"Erreur de connexion à EasyBeer : {e}")
    _all_brassins = []

# Ajouter les 3 derniers brassins archivés (sans doublons)
try:
    _archives = _fetch_brassins_archives()
    _existing_ids = {b.get("idBrassin") for b in _all_brassins}
    for b in _archives:
        if b.get("idBrassin") not in _existing_ids:
            _all_brassins.append(b)
except Exception:
    pass  # pas bloquant si les archives échouent

# Matrice codes-barres
@st.cache_data(ttl=300, show_spinner="Chargement des codes-barres…")
def _fetch_cb_matrix() -> dict[int, list[dict]]:
    raw = get_code_barre_matrice()
    return parse_barcode_matrix(raw)

_cb_by_product: dict[int, list[dict]] | None = None
try:
    _cb_by_product = _fetch_cb_matrix()
except Exception:
    pass

# Poids cartons depuis EasyBeer (cache 1h — les poids changent rarement)
@st.cache_data(ttl=3600, show_spinner="Chargement des poids cartons…")
def _fetch_eb_weights() -> dict[tuple[int, str], float]:
    return fetch_carton_weights()

_eb_weights: dict[tuple[int, str], float] | None = None
try:
    _eb_weights = _fetch_eb_weights()
except Exception:
    pass  # fallback sur les poids statiques

# Entrepot principal
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

# ================================ Selection brassins =========================

_brassins_valides = [b for b in _all_brassins if not b.get("annule")]

if not _brassins_valides:
    st.info("Aucun brassin en cours dans EasyBeer.")
    st.stop()


def _brassin_label(b: dict) -> str:
    nom = b.get("nom", "?")
    prod = clean_product_label((b.get("produit") or {}).get("libelle", "?"))
    vol = b.get("volume", 0)
    tag = ""
    if b.get("archive"):
        tag = " [archivé]"
    elif b.get("termine"):
        tag = " [terminé]"
    return f"{nom} — {prod} — {vol:.0f}L{tag}"


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

# ================================ Construction tableau =======================

meta_by_label: dict = {}
rows: list[dict] = []

if _selected_brassins:
    with st.spinner("Chargement des détails brassins…"):
        rows, meta_by_label = build_ramasse_lines(
            _selected_brassins, _id_entrepot, _cb_by_product, _eb_weights
        )
else:
    st.info("Sélectionne au moins un brassin pour construire la fiche.")

# Table editable
display_cols = [
    "Référence",
    "Produit (goût + format)",
    "DDM",
    "Quantité cartons",
    "Quantité palettes",
    "Poids palettes (kg)",
]
base_df = pd.DataFrame(rows, columns=display_cols) if rows else pd.DataFrame(columns=display_cols)

if not base_df.empty:
    st.caption("Renseigne **Quantité cartons**. Les **palettes** et le **poids** se calculent automatiquement.")
    edited = st.data_editor(
        base_df,
        key="ramasse_editor_v2",
        use_container_width=True,
        hide_index=True,
        column_config={
            "DDM": st.column_config.DateColumn(label="DDM", format="DD/MM/YYYY"),
            "Quantité cartons": st.column_config.NumberColumn(min_value=0, step=1),
            "Quantité palettes": st.column_config.NumberColumn(disabled=True, min_value=0, step=1),
            "Poids palettes (kg)": st.column_config.NumberColumn(disabled=True, format="%.0f"),
        },
    )
else:
    edited = base_df

# ================================ Calculs poids ==============================


def _apply_calculs(df_disp: pd.DataFrame) -> pd.DataFrame:
    out = df_disp.copy()
    palettes_list = []
    poids_list = []
    for _, r in out.iterrows():
        lab = str(r["Produit (goût + format)"]).replace(" - ", " — ")
        meta = meta_by_label.get(lab, meta_by_label.get(str(r["Produit (goût + format)"]), {}))
        pc = float(meta.get("_poids_carton", 0.0))
        pal_cap = int(meta.get("_palette_capacity", 0))
        cartons = int(pd.to_numeric(r["Quantité cartons"], errors="coerce") or 0)

        # Nombre de palettes = arrondi supérieur
        nb_pal = math.ceil(cartons / pal_cap) if pal_cap > 0 and cartons > 0 else 0

        # Poids = (cartons × poids_carton) + (palettes × poids_palette_vide)
        poids_total = int(round(cartons * pc + nb_pal * PALETTE_EMPTY_WEIGHT, 0))

        palettes_list.append(nb_pal)
        poids_list.append(poids_total)
    out["Quantité palettes"] = palettes_list
    out["Poids palettes (kg)"] = poids_list
    return out


df_calc = _apply_calculs(edited)

# ================================ KPIs =======================================

tot_cartons = int(pd.to_numeric(df_calc["Quantité cartons"], errors="coerce").fillna(0).sum())
tot_palettes = int(pd.to_numeric(df_calc["Quantité palettes"], errors="coerce").fillna(0).sum())
tot_poids = int(pd.to_numeric(df_calc["Poids palettes (kg)"], errors="coerce").fillna(0).sum())

c1, c2, c3 = st.columns(3)
with c1:
    kpi("Total cartons", f"{tot_cartons:,}".replace(",", " "))
with c2:
    kpi("Total palettes", f"{tot_palettes}")
with c3:
    kpi("Poids total (kg)", f"{tot_poids:,}".replace(",", " "))

st.dataframe(df_calc[display_cols], use_container_width=True, hide_index=True)

# ================================ PDF ========================================


def _generate_pdf() -> bytes:
    """Genere le PDF BL a la demande."""
    df_for_export = df_calc[display_cols].copy()
    if not pd.api.types.is_string_dtype(df_for_export["DDM"]):
        df_for_export["DDM"] = df_for_export["DDM"].apply(
            lambda d: d.strftime("%d/%m/%Y") if hasattr(d, "strftime") else str(d)
        )
    return build_bl_enlevements_pdf(
        date_creation=today_paris(),
        date_ramasse=date_ramasse,
        destinataire_title=dest_title,
        destinataire_lines=dest_lines,
        df_lines=df_for_export,
    )


if st.button("\U0001F9FE Télécharger la version PDF", use_container_width=True):
    if tot_cartons <= 0:
        st.error("Renseigne au moins une **Quantité cartons** > 0.")
    else:
        try:
            pdf_bytes = _generate_pdf()
            st.session_state["fiche_ramasse_pdf"] = pdf_bytes
            st.download_button(
                "\U0001F4C4 Télécharger la version PDF",
                data=pdf_bytes,
                file_name=f"Fiche_de_ramasse_{date_ramasse:%Y%m%d}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as e:
            st.error(f"Erreur PDF : {e}")

# ================================ Email ======================================


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


def _send_ramasse_email(
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
    logo_niko = _inline_img_from_repo("assets/signature/NIKO_Logo.png")

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

    sender = os.environ.get("EMAIL_SENDER") or os.environ.get("SENDER_EMAIL")
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


# Champ destinataires pre-rempli depuis le JSON
_default_email_str = ", ".join(dest_emails) if dest_emails else ""

if "ramasse_email_to" not in st.session_state or not st.session_state["ramasse_email_to"].strip():
    st.session_state["ramasse_email_to"] = _default_email_str

# Mettre a jour si le destinataire change
if st.session_state.get("_prev_dest") != selected_dest_name:
    st.session_state["ramasse_email_to"] = _default_email_str
    st.session_state["_prev_dest"] = selected_dest_name

to_input = st.text_input(
    "Destinataires (séparés par des virgules)",
    key="ramasse_email_to",
)

sender_hint = os.environ.get("EMAIL_SENDER") or os.environ.get("SENDER_EMAIL")
if sender_hint:
    st.caption(f"Expéditeur utilisé : **{sender_hint}**")


def _parse_emails(s: str) -> list[str]:
    return [e.strip() for e in (s or "").split(",") if e.strip()]


to_list = _parse_emails(st.session_state.get("ramasse_email_to", ""))

if st.button("✉️ Envoyer la demande de ramasse", type="primary", use_container_width=True):
    if tot_cartons <= 0:
        st.error("Le PDF n'est pas prêt et aucun carton n'est saisi. Renseigne au moins une quantité > 0.")
        st.stop()

    # Generer le PDF si pas deja en cache
    pdf_bytes = st.session_state.get("fiche_ramasse_pdf")
    if pdf_bytes is None:
        try:
            pdf_bytes = _generate_pdf()
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

            _send_ramasse_email(
                pdf_bytes=pdf_bytes,
                filename=filename,
                total_palettes=tot_palettes,
                to_list=to_list,
                date_ramasse=date_ramasse,
                bcc_me=True,
            )

            st.success(f"\U0001F4E8 Demande de ramasse envoyée à {len(to_list)} destinataire(s).")
        except Exception as e:
            st.error(f"Échec de l'envoi : {e}")
