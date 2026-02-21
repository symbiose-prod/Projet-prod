from typing import Optional, Dict, Any
import streamlit as st

# ============================ NAV & AUTH BASICS ==============================

USER_KEY = "auth_user"

def current_user() -> Optional[Dict[str, Any]]:
    return st.session_state.get(USER_KEY)

def is_authenticated() -> bool:
    return current_user() is not None

def login_user(user_dict: Dict[str, Any]) -> None:
    st.session_state[USER_KEY] = user_dict

def logout_user() -> None:
    # Révoquer le token persistant en base (si présent)
    token = st.session_state.pop("_fs_session_token", None)
    if token:
        try:
            from common.auth import revoke_session_token
            revoke_session_token(token)
        except Exception:
            pass
    if USER_KEY in st.session_state:
        del st.session_state[USER_KEY]

def _hide_sidebar_nav():
    # Masque le menu des pages tant qu'on n'est pas connecté
    st.markdown("""
        <style>
        section[data-testid="stSidebarNav"] {display:none !important;}
        </style>
    """, unsafe_allow_html=True)

def require_login(redirect_to_auth: bool = True) -> Optional[Dict[str, Any]]:
    """
    À appeler tout en haut de CHAQUE page privée.
    Si non connecté : masque la sidebar + redirige vers pages/00_Auth.py puis stoppe la page.
    """
    u = current_user()
    if u:
        return u

    _hide_sidebar_nav()
    st.error("Veuillez vous connecter pour accéder à cette page.")

    if redirect_to_auth:
        # Redirige vers la page d’auth (toujours relative à l'entrypoint app.py)
        try:
            st.switch_page("pages/00_Auth.py")
        except Exception:
            st.page_link("pages/00_Auth.py", label="Aller à l’authentification", icon="🔐")
    st.stop()
    return None  # pour l'éditeur

def require_role(*roles: str) -> Dict[str, Any]:
    u = require_login()
    if u["role"] not in roles:
        st.error("Accès refusé (rôle insuffisant).")
        st.stop()
    return u


# ====================== SIDEBAR: NAV CUSTOM CONNECTÉ =========================

def sidebar_nav_logged_in():
    """
    Remplace la navigation standard une fois connecté :
    - cache TOUTE la nav multipage de Streamlit
    - affiche notre menu propre, sans 'app' ni 'Auth'
    """
    st.markdown("""
    <style>
      /* Cache toute la nav multipage Streamlit */
      [data-testid="stSidebarNav"]              { display: none !important; }
      [data-testid="stSidebarNavItems"]         { display: none !important; }
      /* Si certaines versions insèrent le bloc nav autrement */
      section[data-testid="stSidebar"] nav      { display: none !important; }
      /* Cache tout lien résiduel vers app.py ou Auth (filet de sécurité) */
      section[data-testid="stSidebar"] a[href$="app.py"],
      section[data-testid="stSidebar"] a[href*="/app"],
      section[data-testid="stSidebar"] a[href*="00_Auth.py"],
      section[data-testid="stSidebar"] a[href*="_00_Auth.py"] { display: none !important; }
    </style>
    """, unsafe_allow_html=True)

    with st.sidebar:
        st.markdown("### Navigation")
        st.page_link("pages/01_Accueil.py",                 label="Accueil",                 icon="🏠")
        st.page_link("pages/02_Production.py",              label="Production",              icon="📦")
        st.page_link("pages/03_Optimisation.py",            label="Optimisation",            icon="🧮")
        st.page_link("pages/04_Fiche_de_ramasse.py",        label="Fiche de ramasse",        icon="🚚")
        st.page_link("pages/05_Achats_conditionnements.py", label="Achats conditionnements", icon="📦")
        st.page_link("pages/99_Debug.py",                   label="Debug",                   icon="🛠️")


def _hide_auth_and_entrypoint_links_when_logged_in():
    # Cache le lien vers la page d’auth + l’entrée "app" dans la nav
    st.markdown("""
    <style>
    /* cache lien vers 00_Auth.py dans la nav */
    section[data-testid="stSidebar"] a[href*="00_Auth.py"] { display: none !important; }
    /* cache le lien d'entrée (app.py) si Streamlit l'affiche */
    section[data-testid="stSidebar"] a[href$="app.py"],
    section[data-testid="stSidebar"] a[href*="app.py?"] { display: none !important; }
    </style>
    """, unsafe_allow_html=True)


def user_menu():
    """
    Encart utilisateur minimal dans la sidebar (nav custom uniquement).
    ⚠️ Ne rend PAS les infos 'Connecté / Rôle / Tenant' (souhaité).
    Le bouton 'Se déconnecter' est géré par user_menu_footer().
    """
    sidebar_nav_logged_in()
    _hide_auth_and_entrypoint_links_when_logged_in()



# ======================== SIDEBAR FOOTER (STICKY) ============================

# Injecte le CSS nécessaire une seule fois
if "_sym_sidebar_css" not in st.session_state:
    st.markdown("""
    <style>
    /* Met la sidebar en colonne et autorise un footer en bas */
    section[data-testid="stSidebar"] div[data-testid="stVerticalBlock"] {
      min-height: 100%;
      display: flex;
      flex-direction: column;
    }
    /* Espace extensible pour repousser le footer */
    .sym-sidebar-spacer { flex-grow: 1; }
    /* Footer visuellement séparé, collé en bas */
    .sym-sidebar-footer {
      position: sticky; bottom: 0;
      background: var(--background-color);
      border-top: 1px solid #e5e7eb;
      padding-top: .75rem; margin-top: .75rem;
    }
    </style>
    """, unsafe_allow_html=True)
    st.session_state["_sym_sidebar_css"] = True


def user_menu_footer(user: Dict[str, Any] | None):
    """
    À appeler en DERNIER dans chaque page, pour garantir qu'il n'y ait rien dessous.
    Rend le bouton de déconnexion + rappel de l'email.
    """
    # espace qui prend toute la hauteur restante pour pousser le footer en bas
    st.sidebar.markdown('<div class="sym-sidebar-spacer"></div>', unsafe_allow_html=True)

    # Cookie manager invisible — nécessaire pour effacer le cookie lors du logout
    _cookie_manager = None
    try:
        import extra_streamlit_components as stx
        _cookie_manager = stx.CookieManager(key="fs_footer_cm")
    except Exception:
        pass

    with st.sidebar:
        st.markdown('<div class="sym-sidebar-footer">', unsafe_allow_html=True)

        # Bouton de déconnexion (clé unique pour éviter les collisions)
        if st.button("Se déconnecter", key="logout_footer", use_container_width=True):
            # Effacer le cookie navigateur si possible
            if _cookie_manager:
                try:
                    _cookie_manager.delete("fs_session")
                except Exception:
                    pass
            logout_user()
            st.success("Déconnecté.")
            st.rerun()

        if user and user.get("email"):
            st.caption(f"Connecté : **{user['email']}**")

        st.markdown('</div>', unsafe_allow_html=True)
