"""
ui/auth.py
==========
Pages d'authentification NiceGUI : login, signup, mot de passe oublié.

Reutilise common/auth.py pour la logique metier.
"""
from __future__ import annotations

import logging

from nicegui import ui, app

from ui.theme import COLORS, apply_quasar_theme, logo_svg
from common.auth import (
    authenticate, create_user, find_user_by_email,
    validate_email, validate_password, check_tenant_allowed,
    create_session_token, SESSION_DEFAULT_DAYS,
)

_log = logging.getLogger("ferment.auth")


# ─── Page Login ─────────────────────────────────────────────────────────────

@ui.page("/login")
def page_login():
    apply_quasar_theme()

    # Si déjà connecté → redirect
    if app.storage.user.get("authenticated"):
        ui.navigate.to("/accueil")
        return

    # Centrage vertical + horizontal
    with ui.column().classes("absolute-center items-center gap-6").style("width: 400px"):

        # Logo / Titre
        with ui.column().classes("items-center gap-2 q-mb-md"):
            ui.html(logo_svg(48, COLORS['green']))
            ui.label("Ferment Station").classes("text-h4 font-bold").style(
                f"color: {COLORS['ink']}"
            )
            ui.label("Connectez-vous pour continuer").classes("text-body2 text-grey-6")

        # Card login
        with ui.card().classes("w-full q-pa-lg").props("flat bordered").style(
            "border-radius: 8px"
        ):
            # Tabs : Connexion / Inscription (2 onglets seulement)
            with ui.tabs().classes("w-full").props(
                'active-color=green-8 indicator-color=green-8'
            ) as tabs:
                tab_login = ui.tab("Connexion", icon="login")
                tab_signup = ui.tab("Inscription", icon="person_add")
                tab_forgot = ui.tab("forgot").classes("hidden")  # caché

            with ui.tab_panels(tabs, value=tab_login).classes("w-full q-mt-md"):

                # ── Tab Connexion ────────────────────────────────────
                with ui.tab_panel(tab_login):
                    email_input = ui.input(
                        "Email",
                        placeholder="prenom.nom@exemple.com",
                    ).classes("w-full q-mb-sm").props("outlined dense")

                    pwd_input = ui.input(
                        "Mot de passe",
                        password=True,
                        password_toggle_button=True,
                    ).classes("w-full q-mb-md").props("outlined dense")

                    remember = ui.checkbox("Se souvenir de moi (30 jours)", value=True)

                    login_error = ui.label("").classes("text-negative text-body2")
                    login_error.set_visibility(False)

                    def do_login():
                        email = email_input.value.strip()
                        pwd = pwd_input.value
                        if not email or not pwd:
                            login_error.text = "Renseigne email et mot de passe."
                            login_error.set_visibility(True)
                            return
                        user = authenticate(email, pwd)
                        if not user:
                            login_error.text = "Identifiants invalides."
                            login_error.set_visibility(True)
                            return
                        # Stocker en session
                        app.storage.user.update({
                            "authenticated": True,
                            "id": str(user["id"]),
                            "tenant_id": str(user["tenant_id"]),
                            "email": user["email"],
                            "role": user.get("role", "user"),
                        })
                        # "Se souvenir de moi" : token stocke via middleware Set-Cookie
                        if remember.value:
                            try:
                                token = create_session_token(
                                    str(user["id"]), str(user["tenant_id"]),
                                    days=SESSION_DEFAULT_DAYS,
                                )
                                # Le middleware posera le cookie HttpOnly sur la prochaine requete
                                app.storage.user["_pending_remember_token"] = token
                            except Exception:
                                _log.warning("Impossible de creer le token remember-me", exc_info=True)
                        ui.navigate.to("/accueil")

                    ui.button(
                        "Connexion",
                        icon="login",
                        on_click=do_login,
                    ).classes("w-full q-mt-sm").props("color=green-8 unelevated")

                    # Lien "Mot de passe oublié ?" sous le bouton
                    ui.button(
                        "Mot de passe oublié ?",
                        on_click=lambda: tabs.set_value(tab_forgot),
                    ).classes("w-full q-mt-xs").props("flat dense color=grey-7 no-caps").style(
                        "font-size: 13px"
                    )

                    # Enter pour valider
                    pwd_input.on("keydown.enter", do_login)

                # ── Tab Inscription ──────────────────────────────────
                with ui.tab_panel(tab_signup):
                    su_email = ui.input("Email").classes("w-full q-mb-sm").props("outlined dense")
                    su_pwd = ui.input("Mot de passe", password=True, password_toggle_button=True).classes("w-full q-mb-sm").props("outlined dense")
                    su_pwd2 = ui.input("Confirmer le mot de passe", password=True, password_toggle_button=True).classes("w-full q-mb-sm").props("outlined dense")
                    su_tenant = ui.input("Organisation (tenant)", placeholder="Ferment Station").classes("w-full q-mb-md").props("outlined dense")

                    signup_msg = ui.label("").classes("text-body2")
                    signup_msg.set_visibility(False)

                    def do_signup():
                        email = su_email.value.strip()
                        pwd = su_pwd.value
                        pwd2 = su_pwd2.value
                        tenant = su_tenant.value.strip()

                        if not all([email, pwd, pwd2, tenant]):
                            signup_msg.text = "Tous les champs sont obligatoires."
                            signup_msg.classes("text-negative")
                            signup_msg.set_visibility(True)
                            return
                        # Validation email
                        try:
                            validate_email(email)
                        except ValueError as ve:
                            signup_msg.text = str(ve)
                            signup_msg.classes("text-negative")
                            signup_msg.set_visibility(True)
                            return
                        # Validation mot de passe
                        try:
                            validate_password(pwd)
                        except ValueError as ve:
                            signup_msg.text = str(ve)
                            signup_msg.classes("text-negative")
                            signup_msg.set_visibility(True)
                            return
                        if pwd != pwd2:
                            signup_msg.text = "Les mots de passe ne correspondent pas."
                            signup_msg.classes("text-negative")
                            signup_msg.set_visibility(True)
                            return
                        if find_user_by_email(email):
                            signup_msg.text = "Un compte existe déjà avec cet email."
                            signup_msg.classes("text-negative")
                            signup_msg.set_visibility(True)
                            return
                        # Vérifier que le tenant est autorisé
                        try:
                            check_tenant_allowed(tenant)
                        except ValueError as ve:
                            signup_msg.text = str(ve)
                            signup_msg.classes("text-negative")
                            signup_msg.set_visibility(True)
                            return

                        try:
                            user = create_user(email, pwd, tenant)
                            app.storage.user.update({
                                "authenticated": True,
                                "id": str(user["id"]),
                                "tenant_id": str(user["tenant_id"]),
                                "email": user["email"],
                                "role": user.get("role", "user"),
                            })
                            ui.navigate.to("/accueil")
                        except ValueError as ve:
                            # Erreurs de validation (email, mot de passe, etc.)
                            signup_msg.text = str(ve)
                            signup_msg.classes("text-negative")
                            signup_msg.set_visibility(True)
                        except Exception:
                            _log.exception("Erreur creation compte")
                            signup_msg.text = "Une erreur est survenue. Réessaie plus tard."
                            signup_msg.classes("text-negative")
                            signup_msg.set_visibility(True)

                    ui.button(
                        "Créer le compte",
                        icon="person_add",
                        on_click=do_signup,
                    ).classes("w-full q-mt-sm").props("color=green-8 unelevated")

                # ── Panel Mot de passe oublié (tab cachée) ──────────
                with ui.tab_panel(tab_forgot):
                    ui.button(
                        "Retour à la connexion",
                        icon="arrow_back",
                        on_click=lambda: tabs.set_value(tab_login),
                    ).classes("q-mb-md").props("flat dense color=grey-7 no-caps").style(
                        "font-size: 13px"
                    )

                    ui.label(
                        "Entrez votre email pour recevoir un lien de réinitialisation."
                    ).classes("text-body2 text-grey-6 q-mb-md")

                    forgot_email = ui.input("Email").classes("w-full q-mb-md").props("outlined dense")
                    forgot_msg = ui.label("").classes("text-body2")
                    forgot_msg.set_visibility(False)

                    def do_forgot():
                        email = forgot_email.value.strip()
                        if not email:
                            forgot_msg.text = "Renseigne ton email."
                            forgot_msg.classes("text-negative")
                            forgot_msg.set_visibility(True)
                            return
                        try:
                            from common.auth_reset import create_password_reset
                            from common.email import send_reset_email
                            reset_url = create_password_reset(email)
                            if reset_url:
                                send_reset_email(email, reset_url)
                            forgot_msg.text = "Si un compte existe, un email a été envoyé."
                            forgot_msg.classes("text-positive")
                            forgot_msg.set_visibility(True)
                        except Exception:
                            _log.exception("Erreur envoi reset email")
                            forgot_msg.text = "Une erreur est survenue. Réessaie plus tard."
                            forgot_msg.classes("text-negative")
                            forgot_msg.set_visibility(True)

                    ui.button(
                        "Envoyer le lien",
                        icon="send",
                        on_click=do_forgot,
                    ).classes("w-full").props("color=green-8 unelevated")


# ─── Page Reset Password ────────────────────────────────────────────────────

@ui.page("/reset/{token}")
def page_reset(token: str):
    """Page publique de reinitialisation du mot de passe."""
    apply_quasar_theme()

    from common.auth_reset import verify_reset_token

    valid, data = verify_reset_token(token)

    with ui.column().classes("absolute-center items-center gap-6").style("width: 400px"):

        # Logo / Titre
        with ui.column().classes("items-center gap-2 q-mb-md"):
            ui.html(logo_svg(48, COLORS['green']))
            ui.label("Ferment Station").classes("text-h4 font-bold").style(
                f"color: {COLORS['ink']}"
            )

        if not valid:
            ui.label(
                "Ce lien de reinitialisation est invalide ou expire."
            ).classes("text-body1 text-negative text-center")
            ui.button(
                "Retour a la connexion",
                icon="arrow_back",
                on_click=lambda: ui.navigate.to("/login"),
            ).props("flat color=green-8")
            return

        user_email = data.get("email", "")
        reset_id = data["reset_id"]
        user_id = str(data["user_id"])

        with ui.card().classes("w-full q-pa-lg").props("flat bordered").style(
            "border-radius: 8px"
        ):
            ui.label("Nouveau mot de passe").classes("text-h6 q-mb-sm").style(
                f"color: {COLORS['ink']}"
            )
            ui.label(f"Compte : {user_email}").classes(
                "text-body2 text-grey-6 q-mb-md"
            )

            new_pwd = ui.input(
                "Nouveau mot de passe",
                password=True, password_toggle_button=True,
            ).classes("w-full q-mb-sm").props("outlined dense")

            confirm_pwd = ui.input(
                "Confirmer le mot de passe",
                password=True, password_toggle_button=True,
            ).classes("w-full q-mb-md").props("outlined dense")

            reset_msg = ui.label("").classes("text-body2")
            reset_msg.set_visibility(False)

            def do_reset():
                pwd = new_pwd.value
                pwd2 = confirm_pwd.value
                if not pwd or not pwd2:
                    reset_msg.text = "Renseigne les deux champs."
                    reset_msg.classes("text-negative")
                    reset_msg.set_visibility(True)
                    return
                if pwd != pwd2:
                    reset_msg.text = "Les mots de passe ne correspondent pas."
                    reset_msg.classes("text-negative")
                    reset_msg.set_visibility(True)
                    return
                try:
                    validate_password(pwd)
                except ValueError as ve:
                    reset_msg.text = str(ve)
                    reset_msg.classes("text-negative")
                    reset_msg.set_visibility(True)
                    return
                try:
                    from common.auth_reset import consume_token_and_set_password
                    consume_token_and_set_password(reset_id, user_id, pwd)
                    reset_msg.text = "Mot de passe modifie ! Redirection..."
                    reset_msg.classes("text-positive")
                    reset_msg.set_visibility(True)
                    ui.timer(2.0, lambda: ui.navigate.to("/login"), once=True)
                except Exception:
                    _log.exception("Erreur reset password")
                    reset_msg.text = "Une erreur est survenue. Reessaie."
                    reset_msg.classes("text-negative")
                    reset_msg.set_visibility(True)

            ui.button(
                "Reinitialiser le mot de passe",
                icon="lock_reset",
                on_click=do_reset,
            ).classes("w-full").props("color=green-8 unelevated")

            confirm_pwd.on("keydown.enter", do_reset)


# ─── Auth guard ─────────────────────────────────────────────────────────────

def require_auth() -> dict:
    """
    Vérifie que l'utilisateur est connecté.
    Retourne le dict user ou redirige vers /login.

    Usage au début de chaque page :
        user = require_auth()
    """
    user = app.storage.user
    if not user.get("authenticated"):
        ui.navigate.to("/login")
        return {}
    return dict(user)
