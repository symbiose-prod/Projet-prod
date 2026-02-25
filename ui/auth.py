"""
ui/auth.py
==========
Pages d'authentification NiceGUI : login, signup, mot de passe oublié.

Réutilise common/auth.py pour la logique métier.
"""
from __future__ import annotations

from nicegui import ui, app

from ui.theme import COLORS, apply_quasar_theme
from common.auth import authenticate, create_user, find_user_by_email


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
            ui.icon("science", size="xl").style(f"color: {COLORS['green']}")
            ui.label("Ferment Station").classes("text-h4 font-bold").style(
                f"color: {COLORS['ink']}"
            )
            ui.label("Connectez-vous pour continuer").classes("text-body2 text-grey-6")

        # Card login
        with ui.card().classes("w-full q-pa-lg").props("flat bordered").style(
            "border-radius: 8px"
        ):
            # Tabs : Connexion / Inscription / Mot de passe
            with ui.tabs().classes("w-full").props(
                'active-color=green-8 indicator-color=green-8 dense no-caps'
            ) as tabs:
                tab_login = ui.tab("Connexion")
                tab_signup = ui.tab("Inscription")
                tab_forgot = ui.tab("Mot de passe oublié")

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
                        ui.navigate.to("/accueil")

                    ui.button(
                        "Connexion",
                        icon="login",
                        on_click=do_login,
                    ).classes("w-full q-mt-sm").props(f'color=green-8 unelevated')

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
                        except Exception as e:
                            signup_msg.text = f"Erreur : {e}"
                            signup_msg.classes("text-negative")
                            signup_msg.set_visibility(True)

                    ui.button(
                        "Créer le compte",
                        icon="person_add",
                        on_click=do_signup,
                    ).classes("w-full q-mt-sm").props("color=green-8 unelevated")

                # ── Tab Mot de passe oublié ──────────────────────────
                with ui.tab_panel(tab_forgot):
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
                        except Exception as e:
                            forgot_msg.text = f"Erreur : {e}"
                            forgot_msg.classes("text-negative")
                            forgot_msg.set_visibility(True)

                    ui.button(
                        "Envoyer le lien",
                        icon="send",
                        on_click=do_forgot,
                    ).classes("w-full").props("color=green-8 unelevated")


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
