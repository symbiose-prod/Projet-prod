"""
ui/theme.py
===========
Charte graphique Ferment Station + layout partagé NiceGUI.

Composants réutilisables : page_layout(), kpi_card(), section_title()
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from nicegui import ui, app

# ─── Logo SVG (cuve + bouteille) ───────────────────────────────────────────

def logo_svg(size: int = 32, color: str = "currentColor") -> str:
    """Retourne le SVG inline du logo Ferment Station (cuve + bouteille)."""
    return f'''<svg width="{size}" height="{size}" viewBox="0 0 48 48" fill="none"
     stroke="{color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
  <!-- Cuve de fermentation -->
  <path d="M6 16 C6 13, 9 10, 16 10 C23 10, 26 13, 26 16"/>
  <rect x="6" y="16" width="20" height="22" rx="3"/>
  <line x1="6" y1="20" x2="26" y2="20"/>
  <circle cx="13" cy="30" r="1.5" fill="{color}" stroke="none"/>
  <circle cx="18" cy="26" r="1" fill="{color}" stroke="none"/>
  <circle cx="11" cy="34" r="1" fill="{color}" stroke="none"/>
  <!-- Tuyau -->
  <path d="M26 28 L32 28"/>
  <!-- Bouteille -->
  <rect x="32" y="26" width="10" height="14" rx="3"/>
  <path d="M35 26 L35 20 L39 20 L39 26"/>
  <line x1="34" y1="18" x2="40" y2="18"/>
  <path d="M35 20 L35 18"/>
  <path d="M39 20 L39 18"/>
</svg>'''


# ─── Couleurs Ferment Station ───────────────────────────────────────────────

COLORS = {
    # Fonds
    "bg":       "#FAFAFA",   # Gris très clair
    "surface":  "#FFFFFF",   # Blanc pur (cards, sidebar)
    "card":     "#FFFFFF",   # Alias backward compat
    # Texte
    "ink":      "#111827",   # Quasi-noir
    "ink2":     "#6B7280",   # Gris secondaire
    # Primaire
    "green":    "#15803D",   # Vert foncé premium
    # Accents
    "sage":     "#E5E7EB",   # Gris neutre (bordures, dividers)
    "orange":   "#F97316",   # Orange moderne
    "lemon":    "#EEDC5B",   # Jaune citron (gardé, peu utilisé)
    # Sémantique
    "error":    "#EF4444",   # Rouge vif
    "success":  "#16A34A",   # Vert succès
    "blue":     "#3B82F6",   # Bleu info
    "warning":  "#F59E0B",   # Ambre warning
    # Bordures
    "border":   "#E5E7EB",   # Gris bordures
}

# ─── Navigation ─────────────────────────────────────────────────────────────

NAV_ITEMS = [
    ("home",           "Accueil",              "/accueil"),
    ("factory",        "Production",           "/production"),
    ("local_shipping", "Fiche de ramasse",     "/ramasse"),
    ("shopping_cart",  "Achats",               "/achats"),
]


# ─── Thème Quasar ───────────────────────────────────────────────────────────

def apply_quasar_theme():
    """Applique le thème Ferment Station — clean / minimaliste."""
    ui.add_head_html(f"""
    <style>
        /* ── Base ──────────────────────────────────── */
        body {{
            background: {COLORS['bg']} !important;
            color: {COLORS['ink']};
            font-family: 'Inter', system-ui, -apple-system, sans-serif;
            -webkit-font-smoothing: antialiased;
        }}

        /* ── Header : vert solide, pas de gradient ── */
        .q-header {{
            background: {COLORS['green']} !important;
            box-shadow: none !important;
        }}

        /* ── Sidebar : blanc pur ────────────────────── */
        .q-drawer {{
            background: {COLORS['surface']} !important;
            border-right: 1px solid {COLORS['border']} !important;
        }}

        /* ── Cards : subtiles ───────────────────────── */
        .q-card {{
            border-radius: 8px !important;
            box-shadow: none !important;
        }}

        /* ── AG Grid : neutre et clean ──────────────── */
        .ag-theme-quartz {{
            --ag-foreground-color: {COLORS['ink']};
            --ag-header-background-color: {COLORS['surface']};
            --ag-odd-row-background-color: {COLORS['bg']};
            --ag-row-hover-color: #F3F4F6;
            --ag-selected-row-background-color: #EFF6FF;
            --ag-font-family: 'Inter', system-ui, sans-serif;
            --ag-font-size: 13px;
            --ag-border-radius: 8px;
            --ag-border-color: {COLORS['border']};
            --ag-header-foreground-color: {COLORS['ink2']};
            --ag-header-cell-hover-background-color: {COLORS['bg']};
        }}

        /* ── KPI cards ──────────────────────────────── */
        .kpi-card {{
            border-radius: 8px;
            border: 1px solid {COLORS['border']};
            transition: border-color 0.15s ease;
        }}
        .kpi-card:hover {{
            border-color: #D1D5DB;
        }}

        /* ── Section headers : bordure minimale ─────── */
        .section-header {{
            border-left: 3px solid {COLORS['border']};
            background: transparent;
            border-radius: 0;
            padding: 8px 12px;
            margin-bottom: 12px;
        }}

        /* ── Nav active : subtil ────────────────────── */
        .nav-active {{
            background: {COLORS['bg']} !important;
            font-weight: 500 !important;
            border-radius: 6px;
        }}

        /* ── Separators ─────────────────────────────── */
        .q-separator {{
            background: {COLORS['border']} !important;
        }}

        /* ── Inputs : coins arrondis ────────────────── */
        .q-field--outlined .q-field__control {{
            border-radius: 6px !important;
        }}

    </style>
    <style>
        /* ── Override Quasar green → palette custom ───── */
        @layer overrides {{
            .bg-green-8, .bg-green-9 {{
                background-color: {COLORS['green']} !important;
            }}
            .text-green-8, .text-green-9 {{
                color: {COLORS['green']} !important;
            }}
        }}
    </style>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    """)


# ─── Composants réutilisables ───────────────────────────────────────────────

def kpi_card(
    icon: str,
    label: str,
    value: str,
    color: str = COLORS["green"],
):
    """Carte KPI minimaliste."""
    with ui.card().classes("kpi-card q-pa-none flex-1").props("flat"):
        with ui.card_section().classes("row items-center gap-3 q-pa-md"):
            with ui.element("div").classes("q-pa-xs").style(
                f"background: {color}10; border-radius: 6px"
            ):
                ui.icon(icon, size="sm").style(f"color: {color}")
            with ui.column().classes("gap-0"):
                ui.label(label).classes("text-caption").style(
                    f"color: {COLORS['ink2']}; font-weight: 500"
                )
                ui.label(value).classes("text-h6").style(
                    f"color: {COLORS['ink']}; font-weight: 600"
                )


def section_title(title: str, icon: str = ""):
    """Titre de section minimaliste."""
    with ui.element("div").classes("section-header row items-center gap-2"):
        if icon:
            ui.icon(icon, size="xs").style(f"color: {COLORS['ink2']}")
        ui.label(title).classes("text-subtitle1").style(
            f"color: {COLORS['ink']}; font-weight: 600"
        )


# ─── Layout partagé ─────────────────────────────────────────────────────────

@contextmanager
def page_layout(title: str, icon: str = "", current_path: str = "/"):
    """
    Context manager pour le layout partagé de toutes les pages.

    Usage :
        @ui.page('/ramasse')
        def page_ramasse():
            with page_layout("Fiche de ramasse", "local_shipping", "/ramasse"):
                ui.label("Contenu ici")
    """
    apply_quasar_theme()

    # ─── Header ──────────────────────────────────────────────────────
    with ui.header().classes("items-center justify-between px-6"):
        with ui.row().classes("items-center gap-3"):
            ui.html(logo_svg(24, "white"))
            ui.label("Ferment Station").classes(
                "text-white text-subtitle1"
            ).style("font-weight: 600")

        with ui.row().classes("items-center gap-3"):
            user = app.storage.user
            email = user.get("email", "")
            initial = email[0].upper() if email else "?"
            ui.label(email).classes("text-white text-body2").style("opacity: 0.85")
            ui.avatar(initial, text_color="white", size="sm").style(
                "background: rgba(255,255,255,0.2)"
            )

    # ─── Drawer (sidebar) ────────────────────────────────────────────
    with ui.left_drawer(value=True, bordered=True).classes("q-pa-md"):

        for nav_icon, nav_label, nav_path in NAV_ITEMS:
            is_active = current_path == nav_path
            btn = ui.button(
                nav_label,
                icon=nav_icon,
                on_click=lambda p=nav_path: ui.navigate.to(p),
            ).classes("w-full justify-start q-mb-xs").props(
                f'flat align=left {"color=green-8" if is_active else "color=grey-8"}'
            ).style("font-size: 13px; text-transform: none; letter-spacing: 0")
            if is_active:
                btn.classes("nav-active")

        ui.separator().classes("q-my-md")

        # Yield slot pour les paramètres sidebar spécifiques à la page
        sidebar_slot = ui.column().classes("w-full gap-2")

        # Spacer + logout
        ui.element("div").style("flex-grow: 1; min-height: 40px")

        ui.separator().classes("q-my-sm")
        ui.button(
            "Se déconnecter",
            icon="logout",
            on_click=_logout,
        ).classes("w-full").props("flat color=grey-8").style(
            "font-size: 13px; text-transform: none"
        )

        if email:
            ui.label(f"Connecté : {email}").classes(
                "text-caption q-mt-xs"
            ).style(f"color: {COLORS['ink2']}")

    # ─── Contenu principal ───────────────────────────────────────────
    with ui.column().classes("w-full max-w-6xl mx-auto q-pa-lg gap-4"):
        # Titre de page
        with ui.row().classes("items-center gap-2 q-mb-xs"):
            if icon:
                ui.icon(icon, size="md").style(f"color: {COLORS['ink']}")
            ui.label(title).classes("text-h5").style(
                f"color: {COLORS['ink']}; font-weight: 600"
            )

        yield sidebar_slot


def _logout():
    """Déconnexion et redirect vers login."""
    app.storage.user.clear()
    ui.navigate.to("/login")
