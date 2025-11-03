# common/storage.py — VERSION DB (run_sql -> list[dict] compatible)
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any, Optional

import pandas as pd  # utilisé pour encoder/décoder les DataFrame dans le JSON
from db.conn import run_sql

# Limite "mémoire longue" par tenant (nombre max de NOMS distincts)
MAX_SLOTS = 6

# Identité par défaut (tu peux changer via variables d'env si tu veux)
DEFAULT_TENANT_NAME = "default"
SYSTEM_EMAIL = "system@symbiose.local"


# ---------- Helpers encodage DataFrame ----------
def _encode_sp(sp: Dict[str, Any]) -> Dict[str, Any]:
    def _df(x):
        return x.to_json(orient="split") if isinstance(x, pd.DataFrame) else None
    return {
        "semaine_du": sp.get("semaine_du"),
        "ddm": sp.get("ddm"),
        "gouts": list(sp.get("gouts", [])),
        "df_min": _df(sp.get("df_min")),
        "df_calc": _df(sp.get("df_calc")),
    }


def _decode_sp(obj: Dict[str, Any]) -> Dict[str, Any]:
    def _df(s):
        return pd.read_json(s, orient="split") if isinstance(s, str) and s.strip() else None
    return {
        "semaine_du": obj.get("semaine_du"),
        "ddm": obj.get("ddm"),
        "gouts": obj.get("gouts") or [],
        "df_min": _df(obj.get("df_min")),
        "df_calc": _df(obj.get("df_calc")),
    }


# ---------- Helpers DB (tenant/user) ----------
def _ensure_tenant(tenant_name: str = DEFAULT_TENANT_NAME) -> str:
    """
    get_or_create tenant (case-insensitive), robuste aux courses.
    """
    n = (tenant_name or "").strip()
    if not n:
        raise ValueError("tenant_name requis")

    rows = run_sql(
        """
        SELECT id FROM tenants
        WHERE lower(name) = lower(:n)
        LIMIT 1
        """,
        {"n": n},
    )
    if rows:
        return rows[0]["id"]

    # Tentative d'INSERT
    try:
        created = run_sql(
            """
            INSERT INTO tenants (id, name, created_at)
            VALUES (gen_random_uuid(), :n, now())
            RETURNING id
            """,
            {"n": n},
        )
        return created[0]["id"]
    except Exception:
        # Course possible → re-SELECT
        again = run_sql(
            """
            SELECT id FROM tenants
            WHERE lower(name) = lower(:n)
            LIMIT 1
            """,
            {"n": n},
        )
        if again:
            return again[0]["id"]
        raise


def _ensure_user(email: str, tenant_id: str) -> str:
    """
    Crée un utilisateur système (si nécessaire) pour les écritures 'techniques'.
    Unicité sur l'email en lower().
    """
    e = (email or "").strip().lower()
    rows = run_sql(
        """
        SELECT id FROM users
        WHERE lower(email) = lower(:e)
        LIMIT 1
        """,
        {"e": e},
    )
    if rows:
        return rows[0]["id"]

    # Insert (mdp désactivé), puis retourner l'id
    created = run_sql(
        """
        INSERT INTO users (id, tenant_id, email, password_hash, role, is_active, created_at)
        VALUES (gen_random_uuid(), :t, :e, '$local$disabled', 'admin', true, now())
        RETURNING id
        """,
        {"t": tenant_id, "e": e},
    )
    return created[0]["id"]


def _tenant_id() -> str:
    """
    Utilise le tenant de l'utilisateur connecté si dispo, sinon fallback sur DEFAULT_TENANT_NAME.
    """
    try:
        from common.session import current_user  # import tardif pour éviter les cycles
        u = current_user()
        if u and u.get("tenant_id"):
            return u["tenant_id"]
    except Exception:
        pass
    return _ensure_tenant(DEFAULT_TENANT_NAME)


def _system_user_id(tenant_id: str) -> str:
    return _ensure_user(SYSTEM_EMAIL, tenant_id)


# ---------- API publique (identique à l’ancienne) ----------
def list_saved() -> List[Dict[str, Any]]:
    """Retourne [{name, ts, gouts, semaine_du}] triés du plus récent au plus ancien (DB)."""
    t_id = _tenant_id()
    rows = run_sql(
        """
        SELECT id, created_at, updated_at, payload
        FROM production_proposals
        WHERE tenant_id = :t
        ORDER BY created_at DESC
        """,
        {"t": t_id},
    )

    out: List[Dict[str, Any]] = []
    for r in rows or []:
        payload = r.get("payload") or {}
        meta = payload.get("_meta", {})
        ts = meta.get("ts")
        if not ts and r.get("created_at"):
            try:
                ts = r["created_at"].isoformat()
            except Exception:
                ts = None
        out.append({
            "name": meta.get("name"),
            "ts": ts,
            "gouts": (payload.get("gouts") or [])[:],
            "semaine_du": payload.get("semaine_du"),
        })
    # si tu préfères trier par meta.ts
    out.sort(key=lambda x: (x.get("ts") or ""), reverse=True)
    return out


def save_snapshot(name: str, sp: Dict[str, Any]) -> Tuple[bool, str]:
    """Crée / remplace une proposition (MAX_SLOTS par tenant basé sur les NOMS distincts)."""
    name = (name or "").strip()
    if not name:
        return False, "Nom vide."

    t_id = _tenant_id()
    u_id = _system_user_id(t_id)

    # construit le payload applicatif + meta horodatée
    payload = _encode_sp(sp)
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload["_meta"] = {"name": name, "ts": ts, "source": "app-db"}

    # Existe déjà ? (match sur _meta.name)
    rows = run_sql(
        """
        SELECT id
        FROM production_proposals
        WHERE tenant_id = :t
          AND payload->'_meta'->>'name' = :n
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        {"t": t_id, "n": name},
    )

    if rows:
        pid = rows[0]["id"]
        run_sql(
            """
            UPDATE production_proposals
            SET payload = CAST(:p AS JSONB), updated_at = NOW()
            WHERE id = :id
            """,
            {"p": json.dumps(payload), "id": pid},
        )
        return True, "Proposition mise à jour."

    # Vérifie la limite MAX_SLOTS sur NOM distinct
    cnt_rows = run_sql(
        """
        SELECT COUNT(DISTINCT payload->'_meta'->>'name') AS c
        FROM production_proposals
        WHERE tenant_id = :t
        """,
        {"t": t_id},
    )
    count = int(cnt_rows[0]["c"]) if cnt_rows else 0
    if count >= MAX_SLOTS:
        return False, f"Limite atteinte ({MAX_SLOTS}). Supprime ou renomme une entrée."

    # Insert (nouvelle entrée)
    run_sql(
        """
        INSERT INTO production_proposals (tenant_id, created_by, payload, status, created_at, updated_at)
        VALUES (:t, :u, CAST(:p AS JSONB), 'draft', now(), now())
        """,
        {"t": t_id, "u": u_id, "p": json.dumps(payload)},
    )

    return True, "Proposition enregistrée."


def load_snapshot(name: str) -> Optional[Dict[str, Any]]:
    t_id = _tenant_id()
    rows = run_sql(
        """
        SELECT payload
        FROM production_proposals
        WHERE tenant_id = :t
          AND payload->'_meta'->>'name' = :n
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        {"t": t_id, "n": name},
    )
    if not rows:
        return None
    payload = rows[0].get("payload") or {}
    return _decode_sp(payload)


def delete_snapshot(name: str) -> bool:
    t_id = _tenant_id()
    rows = run_sql(
        """
        DELETE FROM production_proposals
        WHERE tenant_id = :t
          AND payload->'_meta'->>'name' = :n
        RETURNING id
        """,
        {"t": t_id, "n": name},
    )
    # Avec RETURNING, run_sql renvoie list[dict]
    return bool(rows)


def rename_snapshot(old: str, new: str) -> Tuple[bool, str]:
    new = (new or "").strip()
    if not new:
        return False, "Nouveau nom vide."
    t_id = _tenant_id()

    # existe déjà ?
    exists = run_sql(
        """
        SELECT 1
        FROM production_proposals
        WHERE tenant_id = :t AND payload->'_meta'->>'name' = :n
        LIMIT 1
        """,
        {"t": t_id, "n": new},
    )
    if exists:
        return False, "Ce nom existe déjà."

    rows = run_sql(
        """
        UPDATE production_proposals
        SET payload = jsonb_set(payload, '{_meta,name}', to_jsonb(:new_name::text), true),
            updated_at = NOW()
        WHERE tenant_id = :t
          AND payload->'_meta'->>'name' = :old_name
        RETURNING id
        """,
        {"t": t_id, "old_name": old, "new_name": new},
    )
    if not rows:
        return False, "Entrée introuvable."
    return True, "Renommée."
