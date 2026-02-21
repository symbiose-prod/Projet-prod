# common/auth.py
from __future__ import annotations
import base64
import secrets
import hashlib
import re
import uuid
from typing import Optional, Dict, Any

from db.conn import run_sql  # helper SQL qui renvoie list[dict] pour SELECT


# ------------------------------------------------------------------------------
# PBKDF2 (format: pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>)
# ------------------------------------------------------------------------------
PBKDF2_ALGO = "sha256"
PBKDF2_ITERS = 310_000
SALT_BYTES = 16


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("Mot de passe requis.")
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(PBKDF2_ALGO, password.encode("utf-8"), salt, PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters_s, salt_b64, hash_b64 = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac(PBKDF2_ALGO, password.encode("utf-8"), salt, iters)
        return secrets.compare_digest(dk, expected)
    except Exception:
        return False


# ------------------------------------------------------------------------------
# Tenants (résolution nom <-> UUID) - robustes aux courses d'écriture
# ------------------------------------------------------------------------------
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


def _norm_tenant_name(name: str) -> str:
    # Trim + collapse espaces ; comparaison en lower côté SQL
    return re.sub(r"\s+", " ", (name or "").strip())


def _is_uuid(v: str) -> bool:
    try:
        uuid.UUID(str(v))
        return True
    except Exception:
        return False


def get_tenant_by_name(name: str) -> Optional[Dict[str, Any]]:
    """Recherche insensible à la casse."""
    n = _norm_tenant_name(name)
    rows = run_sql(
        """
        SELECT id, name, created_at
        FROM tenants
        WHERE lower(name) = lower(:n)
        LIMIT 1
        """,
        {"n": n},
    )
    return rows[0] if rows else None


def create_tenant(name: str) -> Dict[str, Any]:
    """Crée le tenant et renvoie (id, name, created_at)."""
    n = _norm_tenant_name(name)
    created = run_sql(
        """
        INSERT INTO tenants (id, name, created_at)
        VALUES (gen_random_uuid(), :n, now())
        RETURNING id, name, created_at
        """,
        {"n": n},
    )
    return created[0]


def get_or_create_tenant(name: str) -> Dict[str, Any]:
    """
    Idempotent et sûr en concurrence :
    - SELECT (lower)
    - INSERT si absent
    - En cas de course (UNIQUE violation), re-SELECT
    """
    n = _norm_tenant_name(name)
    if not n:
        raise ValueError("Tenant name requis.")

    existing = get_tenant_by_name(n)
    if existing:
        return existing

    try:
        return create_tenant(n)
    except Exception:
        # Probable course → le tenant vient d'être créé par un autre worker
        again = get_tenant_by_name(n)
        if again:
            return again
        raise


def ensure_tenant_id(tenant_name_or_id: str) -> str:
    """
    Accepte un UUID ou un nom ; renvoie toujours l'UUID.
    - 'Ferment Station' -> crée/trouve puis renvoie tenants.id (uuid)
    - 'f32b3c7e-....'   -> renvoie tel quel
    """
    t = (tenant_name_or_id or "").strip()
    if not t:
        raise ValueError("Tenant requis.")
    if _is_uuid(t):
        return t
    return get_or_create_tenant(t)["id"]


# ------------------------------------------------------------------------------
# Users
# ------------------------------------------------------------------------------
def find_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    e = (email or "").strip()
    if not e:
        return None
    rows = run_sql(
        """
        SELECT id, tenant_id, email, password_hash, role, is_active, created_at
        FROM users
        WHERE lower(email) = lower(:e)
        LIMIT 1
        """,
        {"e": e},
    )
    return rows[0] if rows else None


def count_users_in_tenant(tenant_id: str) -> int:
    rows = run_sql(
        "SELECT COUNT(*)::int AS n FROM users WHERE tenant_id = :t",
        {"t": tenant_id},
    )
    return int(rows[0]["n"]) if rows else 0


def create_user(email: str, password: str, tenant_name_or_id: str, role: str = None) -> Dict[str, Any]:
    """
    Crée un utilisateur actif.
    - Vérifie l’existence de l’e-mail (insensible à la casse).
    - Résout/crée le tenant.
    - Rôle = 'admin' si premier user du tenant, sinon 'user' (sauf override explicite).
    """
    e = (email or "").strip()
    if not e or not password:
        raise ValueError("email et mot de passe requis.")

    tenant_id = ensure_tenant_id(tenant_name_or_id)

    # Email déjà pris ?
    if find_user_by_email(e):
        raise ValueError("Cet e-mail est déjà utilisé.")

    # Rôle par défaut selon le nombre d'utilisateurs dans le tenant
    computed_role = role or ("admin" if count_users_in_tenant(tenant_id) == 0 else "user")

    created = run_sql(
        """
        INSERT INTO users (id, tenant_id, email, password_hash, role, is_active, created_at)
        VALUES (gen_random_uuid(), :t, lower(:e), :ph, :r, true, now())
        RETURNING id, tenant_id, email, role, is_active, created_at
        """,
        {"t": tenant_id, "e": e, "ph": hash_password(password), "r": computed_role},
    )
    return created[0]


def authenticate(email: str, password: str) -> Optional[Dict[str, Any]]:
    e = (email or "").strip()
    if not e or not password:
        return None
    rows = run_sql(
        """
        SELECT id, tenant_id, email, password_hash, role, is_active, created_at
        FROM users
        WHERE lower(email) = lower(:e)
        LIMIT 1
        """,
        {"e": e},
    )
    if not rows:
        return None
    user = rows[0]
    return user if verify_password(password, user["password_hash"]) else None


def set_user_role(user_id: str, role: str) -> None:
    run_sql(
        "UPDATE users SET role = :r WHERE id = :id",
        {"r": role, "id": user_id},
    )


def change_password(user_id: str, new_password: str) -> None:
    run_sql(
        "UPDATE users SET password_hash = :ph WHERE id = :id",
        {"ph": hash_password(new_password), "id": user_id},
    )


# ------------------------------------------------------------------------------
# Sessions persistantes ("Se souvenir de moi") — tokens stockés en base
# ------------------------------------------------------------------------------
import datetime as _dt
from datetime import timezone as _tz

SESSION_COOKIE = "fs_session"
SESSION_DEFAULT_DAYS = 30


def create_session_token(user_id: str, tenant_id: str, days: int = SESSION_DEFAULT_DAYS) -> str:
    """
    Crée un token de session persistante.
    Retourne le token brut (à stocker dans le cookie navigateur).
    Seul le hash SHA-256 est stocké en base.
    """
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = _dt.datetime.now(_tz.utc) + _dt.timedelta(days=days)
    run_sql(
        """
        INSERT INTO user_sessions (id, user_id, tenant_id, token_hash, expires_at)
        VALUES (gen_random_uuid(), :u, :t, :h, :e)
        """,
        {"u": user_id, "t": tenant_id, "h": token_hash, "e": expires_at},
    )
    return token


def verify_session_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Vérifie le token de session persistante.
    Retourne le user dict si valide (non expiré, utilisateur actif), None sinon.
    """
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    rows = run_sql(
        """
        SELECT u.id, u.tenant_id, u.email, u.role, u.is_active
        FROM user_sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = :h
          AND s.expires_at > now()
          AND u.is_active = true
        LIMIT 1
        """,
        {"h": token_hash},
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "email": row["email"],
        "role": row["role"],
    }


def revoke_session_token(token: str) -> None:
    """Révoque un token de session persistante (utilisé lors du logout)."""
    if not token:
        return
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    run_sql(
        "DELETE FROM user_sessions WHERE token_hash = :h",
        {"h": token_hash},
    )
