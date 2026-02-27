# common/auth_reset.py
from __future__ import annotations
import logging
import os, secrets, hashlib, datetime
from typing import Optional, Dict, Any, Tuple

from db.conn import run_sql

_log = logging.getLogger("ferment.auth_reset")

# URL de base de l'app (OVH VPS)
BASE_URL = os.getenv("BASE_URL", "http://localhost:8502").rstrip("/")
# Durée de validité du lien
RESET_TTL_MINUTES = int(os.getenv("RESET_TTL_MINUTES", "60"))


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _hash_token(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def _recent_requests_for_user(user_id: str):
    """
    Dernières demandes de reset pour anti-spam.
    Retourne une liste de dict (grâce à run_sql -> list[dict]).
    """
    return run_sql(
        """
        SELECT id, created_at, used_at, expires_at
        FROM password_resets
        WHERE user_id = :u
        ORDER BY id DESC
        LIMIT 5
        """,
        {"u": user_id},
    )


def create_password_reset(
    email: str,
    meta: Optional[Dict[str, Any]] = None,
    request_ip: Optional[str] = None,
    request_ua: Optional[str] = None,
) -> Optional[str]:
    """
    Crée une demande de réinitialisation et renvoie l'URL complète.
    Compatible avec :
      - create_password_reset(email, meta={"ip": "...", "ua": "..."})
      - create_password_reset(email, request_ip="...", request_ua="...")
    Si l'email n'existe pas ou si anti-spam → renvoie None silencieusement.
    """
    email = (email or "").strip()
    if not email:
        return None

    # 1) Trouver l'utilisateur
    user_rows = run_sql(
        "SELECT id, email FROM users WHERE lower(email)=lower(:e) LIMIT 1",
        {"e": email},
    )
    if not user_rows:
        # on ne révèle pas si l'email existe
        return None
    user_id = user_rows[0]["id"]

    # Récupère IP/UA quelle que soit la façon d'appeler
    req_ip = request_ip or (meta or {}).get("ip")
    req_ua = request_ua or (meta or {}).get("ua")

    # 2) Anti-spam léger
    recents = _recent_requests_for_user(user_id)
    now = _now_utc()
    active_cnt = 0
    last_created = None
    for r in recents:
        # r est déjà un dict
        if r["used_at"] is None and r["expires_at"] > now:
            active_cnt += 1
        if last_created is None or r["created_at"] > last_created:
            last_created = r["created_at"]

    if active_cnt >= 1:          # max 1 token actif (au lieu de 3)
        _log.info("Reset rate-limited pour user %s : token actif existant", user_id)
        return None
    if last_created and (now - last_created).total_seconds() < 300:  # 5 min (au lieu de 60s)
        _log.info("Reset rate-limited pour user %s : demande trop recente (<5min)", user_id)
        return None

    # 3) Générer token + stocker hash
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    expires_at = now + datetime.timedelta(minutes=RESET_TTL_MINUTES)

    run_sql(
        """
        INSERT INTO password_resets (user_id, token_hash, expires_at, request_ip, request_ua, created_at)
        VALUES (:u, :th, :exp, :ip, :ua, now())
        """,
        {
            "u": user_id,
            "th": token_hash,
            "exp": expires_at,
            "ip": req_ip,
            "ua": req_ua,
        },
    )

    # 4) construire l'URL vers la page de reset (path param, pas query)
    reset_url = f"{BASE_URL}/reset/{token}"
    return reset_url


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Ancien nom — on le garde pour compat.
    Vérifie que le token est valide et non utilisé.
    Renvoie un dict {reset_id, user_id, ...} ou None.
    """
    if not token:
        return None
    token_hash = _hash_token(token)
    rows = run_sql(
        """
        SELECT pr.id AS reset_id,
               pr.user_id,
               pr.expires_at,
               pr.used_at,
               u.email
        FROM password_resets pr
        JOIN users u ON u.id = pr.user_id
        WHERE pr.token_hash = :th
        ORDER BY pr.id DESC
        LIMIT 1
        """,
        {"th": token_hash},
    )
    if not rows:
        return None
    r = rows[0]
    now = _now_utc()
    if r["used_at"] is not None:
        return None
    if r["expires_at"] <= now:
        return None
    return r


def verify_reset_token(token: str) -> Tuple[bool, Any]:
    """
    Nouveau nom, utilisé par la page pages/06_Reset_password.py
    Renvoie (True, {...}) ou (False, "message d'erreur")
    """
    data = verify_token(token)
    if not data:
        return False, "Lien de réinitialisation invalide ou expiré."
    return True, data


def consume_token_and_set_password(reset_id: int, user_id: str, new_password: str) -> bool:
    """
    Transaction atomique : change le mot de passe, revoque les sessions,
    et marque le token comme utilise — tout ou rien.
    """
    from common.auth import validate_password, hash_password
    from db.conn import get_engine
    from sqlalchemy import text as _text

    validate_password(new_password)
    pw_hash = hash_password(new_password)

    with get_engine().begin() as conn:
        # 1) Marquer le token comme utilise (guard anti-race : AND used_at IS NULL)
        result = conn.execute(
            _text("""
                UPDATE password_resets
                SET used_at = now()
                WHERE id = :rid AND user_id = :u AND used_at IS NULL
            """),
            {"rid": reset_id, "u": user_id},
        )
        if result.rowcount == 0:
            raise ValueError("Ce lien de réinitialisation a déjà été utilisé.")

        # 2) Mettre a jour le mot de passe (PBKDF2)
        conn.execute(
            _text("UPDATE users SET password_hash = :ph WHERE id = :uid"),
            {"ph": pw_hash, "uid": user_id},
        )
        # 3) Revoquer toutes les sessions actives (force reconnexion)
        conn.execute(
            _text("DELETE FROM user_sessions WHERE user_id = :u"),
            {"u": user_id},
        )

    return True
