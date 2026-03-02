# common/auth.py
from __future__ import annotations
import base64
import os
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

# ── Validation ────────────────────────────────────────────────────────────────
# Regex durcie : pas de dots consécutifs, pas de dot en début/fin de local part
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9](?:[a-zA-Z0-9._%+\-]*[a-zA-Z0-9])?@[a-zA-Z0-9](?:[a-zA-Z0-9.\-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}$"
)
_EMAIL_MAX_LENGTH = 254  # RFC 5321
_EMAIL_LOCAL_MAX = 64    # RFC 5321
MIN_PASSWORD_LENGTH = 8


def validate_email(email: str) -> str:
    """Valide et normalise l'email (RFC 5321 longueur + format). Lève ValueError si invalide."""
    e = (email or "").strip()
    if not e:
        raise ValueError("Adresse e-mail invalide.")
    if len(e) > _EMAIL_MAX_LENGTH:
        raise ValueError("Adresse e-mail trop longue (254 caractères max).")
    local_part = e.split("@")[0] if "@" in e else e
    if len(local_part) > _EMAIL_LOCAL_MAX:
        raise ValueError("Partie locale de l'e-mail trop longue (64 caractères max).")
    if ".." in e:
        raise ValueError("Adresse e-mail invalide (points consécutifs).")
    if not _EMAIL_RE.match(e):
        raise ValueError("Adresse e-mail invalide.")
    return e


def validate_password(password: str) -> str:
    """Vérifie les règles de complexité du mot de passe. Lève ValueError si invalide."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Le mot de passe doit faire au moins {MIN_PASSWORD_LENGTH} caractères.")
    if password.isdigit():
        raise ValueError("Le mot de passe ne peut pas être uniquement des chiffres.")
    return password


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
    except Exception as exc:
        # Probable course (UNIQUE violation) → le tenant vient d'être créé par un autre worker
        from sqlalchemy.exc import IntegrityError
        if not isinstance(exc.__cause__, IntegrityError) and not isinstance(exc, IntegrityError):
            raise  # erreur inattendue, on la laisse remonter
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


def get_allowed_tenants() -> Optional[list]:
    """
    Retourne la liste des tenants autorisés pour l’inscription, ou None si pas de restriction.
    Variable d’env : ALLOWED_TENANTS (noms séparés par des virgules, insensible à la casse).
    Ex : ALLOWED_TENANTS=Symbiose Kéfir,Ferment Station
    """
    raw = os.environ.get("ALLOWED_TENANTS", "").strip()
    if not raw:
        return None  # pas de restriction (dev local)
    return [t.strip() for t in raw.split(",") if t.strip()]


def check_tenant_allowed(tenant_name: str) -> None:
    """Lève ValueError si le tenant n’est pas dans la whitelist."""
    allowed = get_allowed_tenants()
    if allowed is None:
        return  # pas de restriction configurée
    t = _norm_tenant_name(tenant_name).lower()
    if not any(t == a.lower() for a in allowed):
        raise ValueError("Cette organisation n’accepte pas les inscriptions libres. Contactez un administrateur.")


def create_user(email: str, password: str, tenant_name_or_id: str, role: str = None) -> Dict[str, Any]:
    """
    Crée un utilisateur actif.
    - Vérifie l’existence de l’e-mail (insensible à la casse).
    - Vérifie que le tenant est dans la whitelist (ALLOWED_TENANTS).
    - Résout/crée le tenant.
    - Rôle = ‘admin’ si premier user du tenant, sinon ‘user’ (sauf override explicite).
    """
    e = validate_email(email)
    validate_password(password)

    # Vérifier que le tenant est autorisé avant de le créer
    if not _is_uuid(tenant_name_or_id):
        check_tenant_allowed(tenant_name_or_id)

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


import logging as _logging

_auth_log = _logging.getLogger("ferment.auth")

# Lockout progressif — persisté en base (table login_failures)
_MAX_FAILURES = 5
_LOCKOUT_SECONDS = 300  # 5 min après 5 échecs


def _check_lockout(email_lower: str) -> bool:
    """Retourne True si l'email est en lockout (trop de tentatives récentes)."""
    try:
        rows = run_sql(
            """
            SELECT fail_count, last_fail
            FROM login_failures
            WHERE email = :e
              AND fail_count >= :max
              AND last_fail > now() - make_interval(secs => :secs)
            LIMIT 1
            """,
            {"e": email_lower, "max": _MAX_FAILURES, "secs": _LOCKOUT_SECONDS},
        )
        return bool(rows)
    except Exception:
        _auth_log.warning("Erreur check lockout DB pour %s, on laisse passer", email_lower, exc_info=True)
        return False


def _record_failure(email_lower: str) -> int:
    """Enregistre un échec de login en DB. Retourne le nouveau fail_count."""
    try:
        rows = run_sql(
            """
            INSERT INTO login_failures (email, fail_count, last_fail)
            VALUES (:e, 1, now())
            ON CONFLICT (email) DO UPDATE
              SET fail_count = login_failures.fail_count + 1,
                  last_fail  = now()
            RETURNING fail_count
            """,
            {"e": email_lower},
        )
        return rows[0]["fail_count"] if rows else 0
    except Exception:
        _auth_log.warning("Erreur record failure DB pour %s", email_lower, exc_info=True)
        return 0


def _clear_failures(email_lower: str) -> None:
    """Supprime le compteur d'échecs après un login réussi."""
    try:
        run_sql("DELETE FROM login_failures WHERE email = :e", {"e": email_lower})
    except Exception:
        _auth_log.warning("Erreur clear failures DB pour %s", email_lower, exc_info=True)


def cleanup_expired_failures() -> int:
    """Supprime les entrées de lockout expirées. Retourne le nombre de lignes supprimées."""
    result = run_sql(
        "DELETE FROM login_failures WHERE last_fail < now() - make_interval(secs => :secs) RETURNING email",
        {"secs": _LOCKOUT_SECONDS},
    )
    count = len(result) if isinstance(result, list) else result
    if count:
        _auth_log.info("Nettoyage : %d lockout(s) expiré(s) supprimé(s)", count)
    return count


# Hash factice pour les emails inexistants (timing constant) — lazy pour ne
# pas bloquer le startup avec 310k itérations PBKDF2.
_DUMMY_HASH: str | None = None


def _get_dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("dummy-timing-pad-x7k9")
    return _DUMMY_HASH


def authenticate(email: str, password: str) -> Optional[Dict[str, Any]]:
    e = (email or "").strip()
    if not e or not password:
        return None

    e_lower = e.lower()

    # Vérifier le lockout (persisté en DB)
    if _check_lockout(e_lower):
        _auth_log.warning("Login bloqué (lockout DB) pour %s", e_lower)
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
        # Hash factice pour que le temps de réponse soit constant (anti timing-attack)
        verify_password(password, _get_dummy_hash())
        _record_failure(e_lower)
        _auth_log.info("Echec login : email inconnu %s", e_lower)
        return None

    user = rows[0]
    if verify_password(password, user["password_hash"]):
        # Vérifier que le compte est actif
        if not user.get("is_active", True):
            _auth_log.warning("Login refusé : compte désactivé pour %s", e_lower)
            return None
        # Réinitialiser le compteur en cas de succès
        _clear_failures(e_lower)
        _auth_log.info("Login réussi pour %s", e_lower)
        return user

    # Échec mot de passe
    new_count = _record_failure(e_lower)
    _auth_log.warning("Echec login : mauvais mot de passe pour %s (tentative %d)", e_lower, new_count)
    return None


def change_password(user_id: str, new_password: str) -> None:
    """Change le mot de passe d'un utilisateur (avec validation des règles de complexité)."""
    validate_password(new_password)
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


def cleanup_expired_sessions() -> int:
    """Supprime les sessions expirées. Retourne le nombre de lignes supprimées."""
    result = run_sql("DELETE FROM user_sessions WHERE expires_at < now() RETURNING id")
    count = len(result) if isinstance(result, list) else result
    if count:
        _auth_log.info("Nettoyage : %d session(s) expirée(s) supprimée(s)", count)
    return count


def cleanup_expired_resets() -> int:
    """Supprime les tokens de reset expirés et utilisés. Retourne le nombre de lignes supprimées."""
    result = run_sql(
        "DELETE FROM password_resets WHERE expires_at < now() OR used_at IS NOT NULL RETURNING id"
    )
    count = len(result) if isinstance(result, list) else result
    if count:
        _auth_log.info("Nettoyage : %d token(s) de reset supprimé(s)", count)
    return count
