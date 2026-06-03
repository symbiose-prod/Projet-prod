"""Endpoints mobiles de gestion des accès (admin only).

Permet à un administrateur de gérer les membres de son tenant depuis l'app :
  - GET    /api/v1/admin/users              → liste l'équipe
  - POST   /api/v1/admin/users/invite       → crée un compte + envoie l'invitation
  - PATCH  /api/v1/admin/users/{id}/role    → change le rôle
  - PATCH  /api/v1/admin/users/{id}/active  → active / désactive

Toutes les routes sont strictement admin et scoppées au tenant du token : un
admin ne voit et ne modifie QUE les comptes de sa propre organisation.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import string

from starlette.requests import Request
from starlette.responses import JSONResponse

from common.mobile_v1 import (
    _forbidden,
    _resolve_mobile_user,
    _unauthorized,
)

_log = logging.getLogger("ferment.mobile_v1")


def _require_admin(user: dict | None):
    """Retourne une réponse d'erreur si l'user n'est pas un admin, sinon None."""
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")
    return None


def _serialize_user(row: dict) -> dict:
    """Projette une ligne `users` vers le JSON renvoyé à l'app."""
    created = row.get("created_at")
    return {
        "id": str(row.get("id")),
        "email": row.get("email") or "",
        "role": row.get("role") or "user",
        "is_active": bool(row.get("is_active")),
        "created_at": created.isoformat() if created else None,
    }


def _random_strong_password() -> str:
    """Mot de passe aléatoire (jamais communiqué) pour le compte invité.

    L'invité définira le sien via le lien d'invitation ; ce mot de passe sert
    uniquement de placeholder et doit satisfaire `validate_password`.
    """
    base = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
    return f"Aa1!{base}"


async def _v1_admin_list_users(request: Request):
    """Liste les comptes du tenant. ADMIN only.

    Retour 200 : ``{"users": [{id, email, role, is_active, created_at}, ...]}``.
    """
    user = await _resolve_mobile_user(request)
    err = _require_admin(user)
    if err is not None:
        return err

    from common.auth import list_users_in_tenant

    rows = await asyncio.to_thread(list_users_in_tenant, user["tenant_id"])
    return JSONResponse({"users": [_serialize_user(r) for r in rows]})


async def _v1_admin_invite_user(request: Request):
    """Crée un compte et envoie l'email d'invitation. ADMIN only.

    Body JSON : ``{"email": "...", "role": "user|admin|operateur"}``.

    Retour 200 : ``{"user": {...}, "email_sent": bool}``.
    Retour 400 : email/rôle invalide ou email déjà utilisé.
    Retour 403 : non admin.
    """
    user = await _resolve_mobile_user(request)
    err = _require_admin(user)
    if err is not None:
        return err

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    email = (body.get("email") or "").strip()
    role_raw = (body.get("role") or "operateur").strip()

    from common.auth import create_user, normalize_role, validate_email

    try:
        validate_email(email)
        role = normalize_role(role_raw)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # Création du compte (mot de passe placeholder ; tenant uuid → pas de
    # vérification whitelist). create_user lève ValueError si email déjà pris.
    try:
        created = await asyncio.to_thread(
            create_user,
            email,
            _random_strong_password(),
            user["tenant_id"],
            role,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        _log.exception("Échec création compte invité (mobile)")
        return JSONResponse({"error": "Failed to create account"}, status_code=500)

    # Lien d'invitation + email (best-effort : si l'envoi échoue, le compte
    # existe quand même et l'admin pourra ré-inviter plus tard).
    email_sent = False
    try:
        from common.auth_reset import create_invite_link
        from common.email import send_invite_email

        invite_url = await asyncio.to_thread(create_invite_link, email)
        if invite_url:
            await asyncio.to_thread(
                send_invite_email, email, invite_url, user.get("email"),
            )
            email_sent = True
    except Exception:
        _log.exception("Échec envoi email d'invitation à %s", email)

    return JSONResponse({"user": _serialize_user(created), "email_sent": email_sent})


async def _v1_admin_set_user_role(request: Request):
    """Change le rôle d'un membre du tenant. ADMIN only.

    Path : ``/api/v1/admin/users/{user_id}/role``.
    Body JSON : ``{"role": "user|admin|operateur"}``.

    Retour 200 : ``{"user": {...}}``.
    Retour 400 : rôle invalide, user introuvable, ou garde-fou métier
                 (auto-rétrogradation / dernier admin).
    """
    user = await _resolve_mobile_user(request)
    err = _require_admin(user)
    if err is not None:
        return err

    target_id = request.path_params.get("user_id") or ""
    try:
        body = await request.json()
    except Exception:
        body = {}
    new_role = ((body or {}).get("role") or "").strip()

    from common.auth import update_user_role

    try:
        updated = await asyncio.to_thread(
            update_user_role,
            user["id"], target_id, user["tenant_id"], new_role,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        _log.exception("Échec changement de rôle (mobile)")
        return JSONResponse({"error": "Failed to update role"}, status_code=500)

    return JSONResponse({"user": _serialize_user(updated)})


async def _v1_admin_set_user_active(request: Request):
    """Active ou désactive un compte du tenant. ADMIN only.

    Path : ``/api/v1/admin/users/{user_id}/active``.
    Body JSON : ``{"active": true|false}``.

    Retour 200 : ``{"user": {...}}``.
    Retour 400 : user introuvable, ou garde-fou métier (auto-désactivation /
                 dernier admin).
    """
    user = await _resolve_mobile_user(request)
    err = _require_admin(user)
    if err is not None:
        return err

    target_id = request.path_params.get("user_id") or ""
    try:
        body = await request.json()
    except Exception:
        body = {}
    active = bool((body or {}).get("active"))

    from common.auth import set_user_active

    try:
        updated = await asyncio.to_thread(
            set_user_active,
            user["id"], target_id, user["tenant_id"], active,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        _log.exception("Échec (dés)activation compte (mobile)")
        return JSONResponse({"error": "Failed to update account"}, status_code=500)

    return JSONResponse({"user": _serialize_user(updated)})
