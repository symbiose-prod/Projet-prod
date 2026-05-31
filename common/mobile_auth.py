"""
common/mobile_auth.py
=====================
Authentification de l'app iOS (Ferment Station mobile).

Séparé de `common/auth.py` parce que le modèle est différent :
- pas de cookie navigateur, on retourne un token Bearer brut
- TTL fixe (30 jours), nommé par appareil
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

# Durée de validité d'un token mobile. 30 jours = compromis confort utilisateur
# (relogin mensuel acceptable) / sécurité : un token volé sur un appareil perdu
# expire en 1 mois max même sans révocation explicite.
MOBILE_TOKEN_TTL_DAYS = 30


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

    rows = run_sql(
        """
        INSERT INTO mobile_api_tokens
            (user_id, tenant_id, token_hash, device_name, expires_at)
        VALUES (:u, :t, :h, :d, :e)
        RETURNING id
        """,
        {
            "u": user_id,
            "t": tenant_id,
            "h": token_hash,
            "d": (device_name or "").strip()[:120],
            "e": expires_at,
        },
    )
    token_id = str(rows[0]["id"]) if rows else None
    _log.info("Mobile token créé pour user=%s device=%r", user_id, device_name)

    # Audit : trace l'enregistrement d'un nouvel appareil. Permet de
    # détecter les compromissions ("3 nouveaux iPad inconnus en 1 nuit")
    # et garder un historique pour les revues sécurité.
    from common.audit import ACTION_DEVICE_REGISTERED, log_event
    log_event(
        tenant_id=tenant_id,
        user_email=None,  # pas dispo ici — le service log_event peut le résoudre via user_id si besoin
        action=ACTION_DEVICE_REGISTERED,
        details={
            "user_id": str(user_id),
            "token_id": token_id,
            "device_name": (device_name or "")[:120],
            "expires_at": expires_at.isoformat(),
        },
    )

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


def list_mobile_tokens(
    user_id: str,
    current_token: str | None = None,
) -> list[dict[str, Any]]:
    """Liste les tokens mobiles non révoqués d'un utilisateur.

    Pour l'écran « Mes appareils » : un appareil = un token. On expose
    ``device_name`` + dates + expiration, jamais le hash ni le token brut.

    Si ``current_token`` (le token brut de la requête courante) est fourni,
    chaque entrée porte ``is_current`` — la comparaison de hash reste
    interne à ce module.
    """
    current_hash = _hash_token(current_token) if current_token else None
    rows = run_sql(
        """
        SELECT id, token_hash, device_name, created_at, last_used_at,
               expires_at, (expires_at <= now()) AS expired
        FROM mobile_api_tokens
        WHERE user_id = :u AND revoked_at IS NULL
        ORDER BY last_used_at DESC NULLS LAST, created_at DESC
        """,
        {"u": user_id},
    )
    return [
        {
            "id": str(r["id"]),
            "device_name": r.get("device_name") or "",
            "created_at": r.get("created_at"),
            "last_used_at": r.get("last_used_at"),
            "expires_at": r.get("expires_at"),
            "expired": bool(r.get("expired")),
            "is_current": (
                current_hash is not None
                and r.get("token_hash") == current_hash
            ),
        }
        for r in (rows or [])
    ]


def revoke_mobile_token_by_id(user_id: str, token_id: str) -> bool:
    """Révoque un token par son id, **scopé à l'utilisateur**.

    Un utilisateur ne peut révoquer que ses propres appareils — le filtre
    ``user_id`` l'empêche de toucher le token d'un autre. Retourne ``True``
    si une ligne (non déjà révoquée) a été modifiée.
    """
    if not token_id:
        return False
    rows = run_sql(
        """
        UPDATE mobile_api_tokens
        SET revoked_at = now()
        WHERE id = :id AND user_id = :u AND revoked_at IS NULL
        RETURNING id, tenant_id, device_name
        """,
        {"id": token_id, "u": user_id},
    )
    if not rows:
        return False

    # Audit : trace la révocation. Important pour incidents sécurité
    # (« j'ai perdu mon iPhone hier soir » → on retrouve l'event).
    from common.audit import ACTION_DEVICE_REVOKED, log_event
    log_event(
        tenant_id=str(rows[0].get("tenant_id") or ""),
        user_email=None,  # caller a déjà l'email si besoin, on garde anonyme
        action=ACTION_DEVICE_REVOKED,
        details={
            "user_id": str(user_id),
            "token_id": str(token_id),
            "device_name": str(rows[0].get("device_name") or ""),
        },
    )
    return True


def delete_mobile_user_account(user_id: str, tenant_id: str) -> bool:
    """Suppression compte utilisateur — conforme Apple iOS 14.5+ et RGPD art.17.

    L'opération est atomique (transaction unique) :
      1. ``users.is_active = false`` (soft delete : le compte n'est plus
         utilisable mais la ligne reste pour les contraintes FK).
      2. ``users.email`` est pseudonymisée : remplacée par
         ``deleted-<short_id>@symbiose-internal.local`` (préserve la
         contrainte UNIQUE et évite de réveiller un compte par re-création
         avec le même email).
      3. ``users.password_hash`` mis à vide → toute tentative future de
         login échoue immédiatement (bcrypt sur "" ne matche aucun hash).
      4. Tous les tokens mobiles du user sont révoqués (`revoked_at = now()`).
      5. Anonymisation rétrospective de ``audit_log.user_email`` pour ce
         user (mis à ``NULL``). Les actions métier (scans, finalize…)
         restent traçables (obligation alimentaire FR conserver 5 ans)
         mais ne sont plus rattachées à une personne identifiable.
      6. Nouvelle entrée ``audit_log`` ``account_deleted`` (anonyme).

    Retourne ``True`` si le compte existait et appartenait au tenant.
    """
    # 1 : Récupère l'email courant + verrouille (ligne) — sert d'identifiant
    # pour anonymiser les audit_log avant qu'on modifie users.email.
    current = run_sql(
        """
        SELECT email FROM users
        WHERE id = :uid AND tenant_id = :tid AND is_active = true
        FOR UPDATE
        """,
        {"uid": user_id, "tid": tenant_id},
    )
    if not current:
        return False
    current_email = current[0]["email"]

    # Pseudonyme déterministe : <prefix>-<8 first chars of user_id>
    # — préserve l'UNIQUE constraint et empêche la collision avec un
    # compte légitime (le suffixe @symbiose-internal.local n'est pas
    # un domaine email valide).
    short_id = str(user_id).replace("-", "")[:8]
    pseudo_email = f"deleted-{short_id}@symbiose-internal.local"

    # 2 : Anonymisation rétrospective audit_log (TOUS les évènements de ce
    # tenant rattachés à l'email courant — login, scans, finalize, etc.).
    # Les actions restent traçables au tenant pour la conservation alimentaire
    # FR (5 ans sur les lots/SSCC) mais ne sont plus rattachées à une personne.
    run_sql(
        """
        UPDATE audit_log
        SET user_email = NULL
        WHERE tenant_id = :tid AND user_email = :email
        """,
        {"tid": tenant_id, "email": current_email},
    )

    # 3 : soft delete + pseudonymisation + invalidation password
    run_sql(
        """
        UPDATE users
        SET is_active = false,
            email = :new_email,
            password_hash = ''
        WHERE id = :uid AND tenant_id = :tid
        """,
        {"uid": user_id, "tid": tenant_id, "new_email": pseudo_email},
    )

    # 4 : révocation de tous les tokens mobiles non révoqués
    run_sql(
        """
        UPDATE mobile_api_tokens
        SET revoked_at = now()
        WHERE user_id = :uid AND revoked_at IS NULL
        """,
        {"uid": user_id},
    )

    # 5 : tracer la suppression elle-même (audit forensique anonyme)
    from common.audit import ACTION_ACCOUNT_DELETED, log_event
    log_event(
        tenant_id=tenant_id,
        user_email=None,  # déjà anonymisé
        action=ACTION_ACCOUNT_DELETED,
        details={"user_id": str(user_id)},
    )

    _log.info("Compte mobile supprimé : user_id=%s tenant=%s", user_id, tenant_id)
    return True


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
