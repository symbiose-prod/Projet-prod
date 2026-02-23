import re, unicodedata, os
from io import BytesIO
from PIL import Image
import streamlit as st

COLORS = {
    "bg": "#F7F4EF", "ink": "#2D2A26", "green": "#2F7D5A",
    "sage": "#8BAA8B", "lemon": "#EEDC5B", "card": "#FFFFFF",
}

def apply_theme(page_title="Ferment Station", icon="🥤"):
    st.set_page_config(page_title=page_title, page_icon=icon, layout="wide")
    st.markdown(f"""
    <style>
      .block-container {{ max-width: 1400px; padding-top: 1rem; padding-bottom: 3rem; }}
      h1,h2,h3,h4,h5 {{ color:{COLORS['ink']}; letter-spacing:.2px; }}
      .section-title {{
        display:flex; align-items:center; gap:.5rem; padding:.4rem .8rem;
        background:{COLORS['sage']}22; border-left:6px solid {COLORS['sage']};
        border-radius:14px; margin:.2rem 0 1rem 0;
      }}
      .kpi {{
        background:{COLORS['card']}; border:1px solid #0001;
        border-left:6px solid {COLORS['green']}; border-radius:14px; padding:16px;
      }}
      .kpi .t {{ font-size:.9rem; color:#555; margin-bottom:6px; }}
      .kpi .v {{ font-size:1.5rem; font-weight:700; color:{COLORS['ink']}; }}
      div.stButton > button:first-child {{ background:{COLORS['green']}; color:#fff; border:none; border-radius:12px; }}
    </style>
    """, unsafe_allow_html=True)

def section(title: str, emoji=""):
    t = f"{emoji} {title}" if emoji else title
    st.markdown(f'<div class="section-title"><h2 style="margin:0">{t}</h2></div>', unsafe_allow_html=True)

def kpi(title: str, value: str):
    st.markdown(f'<div class="kpi"><div class="t">{title}</div><div class="v">{value}</div></div>', unsafe_allow_html=True)

# ---------- Images helpers ----------
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

def slugify(s: str) -> str:
    s = str(s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s

def find_image_path(images_dir: str, sku: str = None, flavor: str = None):
    """
    Ordre:
      0) assets/image_map.csv (canonical -> filename). Si filename sans extension, on essaie .jpg/.jpeg/.png/.webp/.gif
      1) Par SKU (CITR-33.ext puis CITR.ext)
      2) Par slug du goût (ex: mangue-passion.ext)
    """
    import os, csv, unicodedata, re as _re

    def _norm_key(s: str) -> str:
        s = str(s or "")
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
        s = _re.sub(r"\s+", " ", s).strip().lower()
        return s

    # 0) mapping CSV
    map_csv = os.path.join(images_dir, "image_map.csv")
    if os.path.exists(map_csv) and flavor:
        for sep in (",", ";"):
            try:
                d = {}
                with open(map_csv, "r", encoding="utf-8") as f:
                    rdr = csv.DictReader(f, delimiter=sep)
                    if not rdr.fieldnames:
                        continue
                    cols = {c.lower(): c for c in rdr.fieldnames}
                    if "canonical" in cols and "filename" in cols:
                        for row in rdr:
                            cano = (row.get(cols["canonical"]) or "").strip()
                            fn   = (row.get(cols["filename"])  or "").strip()
                            if cano and fn:
                                d[_norm_key(cano)] = fn
                        break
            except Exception:
                pass
        fn = d.get(_norm_key(flavor)) if 'd' in locals() else None
        if fn:
            p = os.path.join(images_dir, fn)
            if os.path.splitext(fn)[1] == "":  # pas d'extension
                for ext in IMG_EXTS:
                    p_try = p + ext
                    if os.path.exists(p_try):
                        return p_try
            if os.path.exists(p):
                return p

    # 1) SKU
    if sku:
        for ext in IMG_EXTS:
            p = os.path.join(images_dir, f"{sku}{ext}")
            if os.path.exists(p):
                return p
        base_root = _re.sub(r"-\d+$", "", sku)
        for ext in IMG_EXTS:
            p = os.path.join(images_dir, f"{base_root}{ext}")
            if os.path.exists(p):
                return p

    # 2) slug du goût
    if flavor:
        from .design import slugify  # si slugify est dans ce fichier, sinon adapte
        s = slugify(flavor)
        for ext in IMG_EXTS:
            p = os.path.join(images_dir, f"{s}{ext}")
            if os.path.exists(p):
                return p

    return None


import os, base64
from io import BytesIO
from PIL import Image

def load_image_bytes(path: str):
    """
    Retourne :
    - bytes PNG (préféré)
    - ou data-URL base64 (fallback)
    """
    if not path or not os.path.exists(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    try:
        im = Image.open(path).convert("RGBA")
        buf = BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        try:
            with open(path, "rb") as f:
                raw = f.read()
            mime = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
            }.get(ext, "image/octet-stream")
            b64 = base64.b64encode(raw).decode("ascii")
            return f"data:{mime};base64,{b64}"
        except Exception:
            return None
