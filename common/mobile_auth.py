"""
common/mobile_auth.py
=====================
Authentification de l'app iOS (Ferment Station mobile).

Séparé de `common/auth.py` parce que le modèle est différent :
- pas de cookie navigateur, on retourne un token Bearer brut
- TTL plus long (90 jours), nommé par appareil
- `last_used_at` mis à jour à chaque appel (audit + révocation ciblée)

Ne dépend PAS de NiceGUI — testable en isolation.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import secrets
from typing import Any

from db.conn import run_sql

_log = logging.getLogger("ferment.mobile_auth")

# Durée de validité d'un token mobile. 90 jours = compromis confort utilisateur
# (pas de relogin permanent) / sécurité (révocation possible côté serveur).
MOBILE_TOKEN_TTL_DAYS = 90


def _hash_token(token: str) -> str:
    """SHA-256 du token brut — on ne stocke jamais le token en clair."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_mobile_token(
    user_id: str,
    tenant_id: str,
    device_name: str = "",
    ttl_days: int = MOBILE_TOKEN_TTL_DAYS,
) -> tuple[str, _dt.datetime]:
    """Crée un token API mobile et le retourne EN CLAIR (seule occasion).

    Seul le hash SHA-256 est persisté. Le caller doit transmettre la valeur
    retournée à l'app iOS, qui la stockera dans le Keychain.

    `device_name` : libre, fourni par le client iOS ("iPhone Nicolas", "iPad
    Symbiose"). Permet à l'utilisateur de révoquer un appareil précis plus
    tard sans toucher aux autres.

    Retourne `(token, expires_at)` pour que l'endpoint puisse renvoyer la
    date d'expiration à l'app sans la recalculer.
    """
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    expires_at = _dt.datetime.now(_dt.UTC) + _dt.timedelta(days=ttl_days)

    run_sql(
        """
        INSERT INTO mobile_api_tokens
            (user_id, tenant_id, token_hash, device_name, expires_at)
        VALUES (:u, :t, :h, :d, :e)
        """,
        {
            "u": user_id,
            "t": tenant_id,
            "h": token_hash,
            "d": (device_name or "").strip()[:120],
            "e": expires_at,
        },
    )
    _log.info("Mobile token créé pour user=%s device=%r", user_id, device_name)
    return token, expires_at


def verify_mobile_token(token: str) -> dict[str, Any] | None:
    """Valide un token brut → renvoie `{id, tenant_id, email, role}` ou None.

    Touche `last_used_at` à chaque vérification réussie (best-effort —
    une erreur DB sur le UPDATE ne doit pas bloquer l'API).
    """
    if not token or not isinstance(token, str):
        return None
    token_hash = _hash_token(token)

    rows = run_sql(
        """
        SELECT u.id, u.tenant_id, u.email, u.role, t.id AS token_id
        FROM mobile_api_tokens t
        JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = :h
          AND t.expires_at > now()
          AND t.revoked_at IS NULL
          AND u.is_active = true
        LIMIT 1
        """,
        {"h": token_hash},
    )
    if not rows:
        return None

    row = rows[0]

    # Best-effort : touch `last_used_at` (utile pour audit, pas critique).
    try:
        run_sql(
            "UPDATE mobile_api_tokens SET last_used_at = now() WHERE id = :id",
            {"id": row["token_id"]},
        )
    except Exception:
        _log.exception("Echec UPDATE last_used_at pour token id=%s", row["token_id"])

    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "email": row["email"],
        "role": row["role"],
    }


def revoke_mobile_token(token: str) -> bool:
    """Révoque un token brut. Retourne True si une ligne a été modifiée."""
    if not token:
        return False
    token_hash = _hash_token(token)
    rows = run_sql(
        """
        UPDATE mobile_api_tokens
        SET revoked_at = now()
        WHERE token_hash = :h AND revoked_at IS NULL
        RETURNING id
        """,
        {"h": token_hash},
    )
    return bool(rows)


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Extrait le token d'un header `Authorization: Bearer <token>`.

    Retourne None si le header est absent, vide, ou mal formé.
    """
    if not authorization_header:
        return None
    parts = authorization_header.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None
