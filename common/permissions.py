"""
common/permissions.py
=====================
Source unique de vérité pour le contrôle d'accès basé sur les rôles (RBAC).

Rôles :
  - ``admin``    : accès à tout (par défaut pour le premier utilisateur d'un tenant)
  - ``user``     : accès à tout sauf les pages admin (legacy, par défaut signup)
  - ``operateur``: accès limité — uniquement étiquettes palette + ramasse

Pour ajouter une page à l'accès opérateur, ajoute son chemin à
``OPERATEUR_ALLOWED_PATHS`` ci-dessous.

Pour ajouter un nouveau rôle, étends ``can_access_path()`` avec une nouvelle
règle. Garder ce fichier court — c'est le seul endroit où la matrice
d'accès est définie.
"""
from __future__ import annotations

# Chemins (prefixes) accessibles à un opérateur. Le matching utilise startswith
# pour couvrir les sous-routes (ex: /etiquettes-palette/quelque-chose).
OPERATEUR_ALLOWED_PATHS: tuple[str, ...] = (
    "/etiquettes-palette",
    "/chargement-camion",
    "/historique-ramasses",
    "/logout",       # toujours possible de se déconnecter
    "/api/logout",   # idem
)

# Chemins strictement réservés aux admins (refus pour user et operateur).
# Le middleware refuse l'accès AVANT d'atteindre la page — défense en
# profondeur en plus du _require_admin() interne à certaines pages.
ADMIN_ONLY_PATHS: tuple[str, ...] = (
    "/admin",
    "/sscc-log",
    "/test-carton-counter",
)

# Page d'accueil par défaut selon le rôle, utilisée :
#   - après login réussi
#   - quand un opérateur tape une URL hors de sa zone
ROLE_HOME_PAGE: dict[str, str] = {
    "admin": "/accueil",
    "user": "/accueil",
    "operateur": "/etiquettes-palette",
}

_DEFAULT_HOME = "/accueil"


def home_page_for_role(role: str | None) -> str:
    """Retourne la route d'accueil à utiliser après login pour ce rôle."""
    return ROLE_HOME_PAGE.get((role or "user").strip().lower(), _DEFAULT_HOME)


def _matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    """True si path est exactement l'un des prefixes ou une sous-route."""
    return any(
        path == p or path.startswith(p + "/") or path.startswith(p + "?")
        for p in prefixes
    )


def can_access_path(role: str | None, path: str) -> bool:
    """Retourne True si le rôle a le droit d'accéder à ce path.

    Règle :
      - ``admin`` accède à tout
      - ``operateur`` accède uniquement aux paths listés dans
        OPERATEUR_ALLOWED_PATHS (ou leurs sous-routes)
      - ``user`` accède à tout sauf les ADMIN_ONLY_PATHS
      - tout autre rôle inconnu : accès comme ``user``
    """
    role_norm = (role or "user").strip().lower()
    if role_norm == "admin":
        return True
    # Pages strictement admin : refusées pour tous les autres rôles
    if _matches_prefix(path, ADMIN_ONLY_PATHS):
        return False
    if role_norm == "operateur":
        return _matches_prefix(path, OPERATEUR_ALLOWED_PATHS)
    # user et autres : tout passe sauf admin-only (déjà filtré au-dessus)
    return True


def is_nav_visible(role: str | None, path: str) -> bool:
    """Retourne True si le lien de menu doit apparaître dans la sidebar.

    Identique à can_access_path mais avec une sémantique UI explicite
    (au cas où on voudrait diverger un jour — par ex. afficher un lien
    grisé même si l'accès est refusé).
    """
    return can_access_path(role, path)
