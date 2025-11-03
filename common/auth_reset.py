# common/auth_reset.py
from __future__ import annotations
import os, secrets, hashlib, datetime
from typing import Optional, Dict, Any
from db.conn import run_sql

BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
RESET_TTL_MINUTES = int(os.getenv("RESET_TTL_MINUTES", "60"))

def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)

def _hash_token(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()

def _recent_requests_for_user(user_id: str) -> Dict[str, Any]:
    rows = run_sql("""
        SELECT id, created_at, used_at, expires_at
        FROM password_resets
        WHERE user_id = :u
        ORDER BY id DESC
        LIMIT 5
    """, {"u": user_id})
    return rows

def create_password_reset(email: str, request_ip: str = None, request_ua: str = None) -> Optional[str]:
    # 1) Trouver l'utilisateur
    user = run_sql("""
        SELECT id, email FROM users WHERE lower(email)=lower(:e) LIMIT 1
    """, {"e": (email or "").strip()})
    if not user:
        # Sécurité : ne révèle pas l'existence ou non de l'email
        return None
    user_id = user[0]["id"]

    # 2) Anti-spam léger
    recents = _recent_requests_for_user(user_id)
    active_cnt = 0
    last_created = None
    now = _now_utc()
    for r in recents:
        if r["used_at"] is None and r["expires_at"] > now:
            active_cnt += 1
        if last_created is None or r["created_at"] > last_created:
            last_created = r["created_at"]

    if active_cnt >= 3:
        # Trop de resets actifs
        return None
    if last_created and (now - last_created).total_seconds() < 60:
        # 60s entre deux demandes
        return None

    # 3) Générer token (non stocké en clair en DB)
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    expires_at = now + datetime.timedelta(minutes=RESET_TTL_MINUTES)

    created = run_sql("""
        INSERT INTO password_resets (user_id, token_hash, expires_at, request_ip, request_ua, created_at)
        VALUES (:u, :th, :exp, :ip, :ua, now())
        RETURNING id
    """, {"u": user_id, "th": token_hash, "exp": expires_at, "ip": request_ip, "ua": request_ua})

    reset_url = f"{BASE_URL}/06_Reset_password?token={token}"
    return reset_url

def verify_token(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    token_hash = _hash_token(token)
    rows = run_sql("""
        SELECT pr.id AS reset_id, pr.user_id, pr.expires_at, pr.used_at, u.email
        FROM password_resets pr
        JOIN users u ON u.id = pr.user_id
        WHERE pr.token_hash = :th
        ORDER BY pr.id DESC
        LIMIT 1
    """, {"th": token_hash})
    if not rows:
        return None
    r = rows[0]
    now = _now_utc()
    if r["used_at"] is not None:
        return None
    if r["expires_at"] <= now:
        return None
    return r

def consume_token_and_set_password(reset_id: int, user_id: str, new_password: str) -> bool:
    # 1) Mettre à jour le mot de passe
    from werkzeug.security import generate_password_hash
    pwd_hash = generate_password_hash(new_password)

    run_sql("""
        UPDATE users SET password_hash=:p WHERE id=:u
    """, {"p": pwd_hash, "u": user_id})

    # 2) Marquer le token comme utilisé
    run_sql("""
        UPDATE password_resets
        SET used_at = now()
        WHERE id = :rid AND user_id = :u
    """, {"rid": reset_id, "u": user_id})

    return True
