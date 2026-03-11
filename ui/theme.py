"""
ui/theme.py
===========
Charte graphique Ferment Station + layout partagé NiceGUI.

Composants réutilisables : page_layout(), kpi_card(), section_title()
"""
from __future__ import annotations

from contextlib import contextmanager

from nicegui import app, ui

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
    ("inventory_2",    "Stocks",               "/stocks"),
    ("menu_book",      "Ressources",           "/ressources"),
    ("label",          "Étiquettes",           "/sync"),
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

        /* ── Quasar Table : clean style ────────────── */
        .q-table {{
            font-family: 'Inter', system-ui, sans-serif;
            font-size: 13px;
            color: {COLORS['ink']};
        }}
        .q-table thead th {{
            color: {COLORS['ink2']} !important;
            font-weight: 600;
            font-size: 12px;
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

        /* ── Mobile : padding réduit ──────────────────── */
        @media (max-width: 599px) {{
            .q-page-container .q-pa-lg {{
                padding: 12px !important;
            }}
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
    <!-- PWA -->
    <link rel="manifest" href="/static/manifest.json">
    <meta name="theme-color" content="#15803D">
    <!-- iOS Safari -->
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="Ferment Station">
    <link rel="apple-touch-icon" href="/static/icons/icon-192.png">
    <!-- Favicon -->
    <link rel="icon" type="image/png" sizes="32x32" href="/static/icons/favicon-32.png">
    <!-- Service Worker -->
    <script>
    if ('serviceWorker' in navigator) {{
      navigator.serviceWorker.register('/service-worker.js');
    }}
    </script>
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
                ).props('aria-live="polite"')


def date_picker_field(default_value: str, label: str | None = None) -> ui.input:
    """
    Composant réutilisable : input date + menu popup + date picker Quasar.

    Retourne le ui.input (sa .value contient la date ISO sélectionnée).
    """
    if label:
        ui.label(label).classes("text-subtitle2 q-mb-xs").style(
            f"color: {COLORS['ink']}; font-weight: 600"
        )
    date_input = ui.input(value=default_value).props("outlined dense").classes("w-full")
    with date_input:
        with ui.menu().props("no-parent-event") as menu:
            picker = ui.date(value=default_value).props("dense first-day-of-week=1")
            picker.on_value_change(
                lambda e: (date_input.set_value(e.value), menu.close())
            )
        with date_input.add_slot("append"):
            ui.icon("event", size="xs").classes("cursor-pointer").on("click", lambda: menu.open())
    return date_input


@contextmanager
def loading_overlay(container, message: str = "Chargement…"):
    """Affiche un overlay semi-transparent + spinner + message dans un conteneur NiceGUI.

    Usage ::

        with loading_overlay(my_card, "Analyse du fichier en cours…"):
            await asyncio.to_thread(heavy_task)
        # L'overlay est automatiquement retiré à la sortie du context manager.
    """
    overlay = None
    with container:
        overlay = ui.element("div").style(
            "position: absolute; inset: 0; z-index: 10; "
            "background: rgba(255,255,255,0.8); "
            "display: flex; flex-direction: column; "
            "align-items: center; justify-content: center; gap: 12px; "
            "border-radius: 8px"
        )
        with overlay:
            ui.spinner("dots", size="lg", color="green-8")
            ui.label(message).classes("text-body2").style(f"color: {COLORS['ink2']}")
    try:
        yield overlay
    finally:
        if overlay:
            try:
                overlay.delete()
            except Exception:
                pass


def password_strength_bar(password_input: ui.input) -> ui.element:
    """Ajoute un indicateur de force de mot de passe sous un champ NiceGUI.

    Retourne le conteneur de l'indicateur (pour le positionner).
    Met à jour en live à chaque frappe.
    """
    from common.auth import MIN_PASSWORD_LENGTH

    container = ui.column().classes("w-full gap-0 q-mt-none")
    with container:
        bar = ui.linear_progress(value=0, show_value=False, size="6px").props("rounded color=grey-4").classes("w-full")
        rules_row = ui.row().classes("w-full gap-4 q-mt-xs")
        with rules_row:
            lbl_len = ui.label(f"✗ {MIN_PASSWORD_LENGTH}+ caractères").classes("text-caption").style("color: #EF4444")
            lbl_letter = ui.label("✗ Contient une lettre").classes("text-caption").style("color: #EF4444")
            lbl_digit = ui.label("✗ Contient un chiffre").classes("text-caption").style("color: #EF4444")

    def _update(e):
        pwd = e.value or ""
        ok_len = len(pwd) >= MIN_PASSWORD_LENGTH
        ok_letter = any(c.isalpha() for c in pwd)
        ok_digit = any(c.isdigit() for c in pwd)
        score = sum([ok_len, ok_letter, ok_digit])

        # Barre de progression + couleur
        bar.set_value(score / 3)
        color_map = {0: "grey-4", 1: "red-6", 2: "orange-6", 3: "green-7"}
        bar.props(f'color={color_map[score]}')

        # Labels
        lbl_len.text = f"{'✓' if ok_len else '✗'} {MIN_PASSWORD_LENGTH}+ caractères"
        lbl_len.style(f"color: {'#16A34A' if ok_len else '#EF4444'}")
        lbl_letter.text = f"{'✓' if ok_letter else '✗'} Contient une lettre"
        lbl_letter.style(f"color: {'#16A34A' if ok_letter else '#EF4444'}")
        lbl_digit.text = f"{'✓' if ok_digit else '✗'} Contient un chiffre"
        lbl_digit.style(f"color: {'#16A34A' if ok_digit else '#EF4444'}")

    password_input.on("update:model-value", _update)
    return container


def section_title(title: str, icon: str = ""):
    """Titre de section minimaliste."""
    with ui.element("div").classes("section-header row items-center gap-2"):
        if icon:
            ui.icon(icon, size="xs").style(f"color: {COLORS['ink2']}")
        ui.label(title).classes("text-subtitle1").style(
            f"color: {COLORS['ink']}; font-weight: 600"
        )


def confirm_dialog(
    title: str,
    message: str,
    action_label: str = "Confirmer",
    action_icon: str = "",
    danger: bool = False,
) -> tuple:
    """Dialogue de confirmation réutilisable.

    Retourne (dialog, message_label, action_button) pour permettre
    la personnalisation dynamique du message et du handler.

    Usage ::

        dlg, msg_lbl, action_btn = confirm_dialog(
            "Confirmer ?", "Ceci est irréversible.", "Supprimer", danger=True,
        )
        action_btn.on_click(lambda: (dlg.close(), my_action()))
        trigger_btn = ui.button("Ouvrir", on_click=dlg.open)
    """
    action_color = "red-7" if danger else "green-8"
    with ui.dialog() as dlg, ui.card().classes("q-pa-lg"):
        ui.label(title).classes("text-subtitle1").style(
            f"color: {COLORS['ink']}; font-weight: 600"
        )
        msg_label = ui.label(message).classes("text-body2 text-grey-7 q-mt-xs")
        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            ui.button("Annuler", on_click=dlg.close).props("flat color=grey-7")
            props = f"color={action_color} unelevated"
            if action_icon:
                action_btn = ui.button(action_label, icon=action_icon).props(props)
            else:
                action_btn = ui.button(action_label).props(props)
    return dlg, msg_label, action_btn


def error_banner(
    message: str,
    retry_fn=None,
    dismissible: bool = True,
) -> ui.card:
    """Bannière d'erreur unifiée avec retry optionnel.

    Retourne la card pour manipulation ultérieure (ex: card.delete()).
    """
    with ui.card().classes("w-full").props("flat bordered").style(
        f"border-color: {COLORS['error']}40"
    ) as card:
        with ui.card_section().classes("row items-center gap-3 q-pa-md"):
            ui.icon("error_outline", size="sm").style(f"color: {COLORS['error']}")
            ui.label(message).classes("text-body2 flex-1").style(
                f"color: {COLORS['error']}"
            )
            if retry_fn:
                ui.button(
                    "Réessayer", icon="refresh", on_click=retry_fn,
                ).props("outline color=red-7 dense")
            if dismissible:
                ui.button(
                    icon="close", on_click=lambda: card.delete(),
                ).props("flat round dense color=grey-6")
    return card


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
    with ui.header().classes("items-center justify-between px-4 px-sm-6"):
        with ui.row().classes("items-center gap-2"):
            # Hamburger menu (visible sur mobile, caché sur desktop)
            menu_btn = ui.button(icon="menu", on_click=lambda: drawer.toggle()).props(
                "flat round dense color=white"
            ).classes("lt-md")
            ui.html(logo_svg(24, "white"))
            ui.label("Ferment Station").classes(
                "text-white text-subtitle1 gt-xs"
            ).style("font-weight: 600")

        with ui.row().classes("items-center gap-2"):
            user = app.storage.user
            email = user.get("email", "")
            initial = email[0].upper() if email else "?"
            ui.label(email).classes("text-white text-body2 gt-sm").style("opacity: 0.85")
            ui.avatar(initial, text_color="white", size="sm").style(
                "background: rgba(255,255,255,0.2)"
            )

    # ─── Drawer (sidebar) ────────────────────────────────────────────
    # breakpoint=768 : en overlay sur mobile/tablette, persistant sur desktop
    with ui.left_drawer(value=True, bordered=True).props("breakpoint=768").classes("q-pa-md") as drawer:

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
    """Deconnexion : clear session + redirect vers /api/logout (cookie HttpOnly)."""
    app.storage.user.clear()
    # /api/logout revoque le token DB, supprime le cookie, et redirige vers /login
    ui.navigate.to("/api/logout")
