# common/storage.py — VERSION DB (run_sql -> list[dict] compatible)
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

_log = logging.getLogger("ferment.storage")

import pandas as pd  # utilisé pour encoder/décoder les DataFrame dans le JSON

# Limite "mémoire longue" par tenant (chargée depuis config.yaml)
from common.data import get_business_config as _get_biz
from db.conn import run_sql

MAX_SLOTS: int = _get_biz()["max_slots"]

# Identité par défaut (tu peux changer via variables d'env si tu veux)
DEFAULT_TENANT_NAME = "default"
SYSTEM_EMAIL = "system@symbiose.local"


# ---------- Helpers encodage DataFrame ----------
def _encode_sp(sp: dict[str, Any]) -> dict[str, Any]:
    def _df(x):
        return x.to_json(orient="split") if isinstance(x, pd.DataFrame) else None
    return {
        "semaine_du": sp.get("semaine_du"),
        "ddm": sp.get("ddm"),
        "gouts": list(sp.get("gouts", [])),
        "df_min": _df(sp.get("df_min")),
        "df_calc": _df(sp.get("df_calc")),
    }


def _decode_sp(obj: dict[str, Any]) -> dict[str, Any]:
    def _df(s):
        if not isinstance(s, str) or not s.strip():
            return None
        import io
        return pd.read_json(io.StringIO(s), orient="split")
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
    except Exception as exc:
        # Course possible (UNIQUE violation) → re-SELECT
        from sqlalchemy.exc import IntegrityError
        if not isinstance(exc.__cause__, IntegrityError) and not isinstance(exc, IntegrityError):
            raise  # erreur inattendue
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
    Utilise le tenant de l'utilisateur connecté (NiceGUI storage) si dispo,
    sinon fallback sur DEFAULT_TENANT_NAME (CLI, scripts, startup).
    """
    try:
        from nicegui import app  # import tardif pour éviter les cycles
        tid = app.storage.user.get("tenant_id")
        if tid:
            return str(tid)
    except Exception:
        _log.debug("Erreur lecture tenant depuis session, fallback default", exc_info=True)
    return _ensure_tenant(DEFAULT_TENANT_NAME)


def _system_user_id(tenant_id: str) -> str:
    return _ensure_user(SYSTEM_EMAIL, tenant_id)


# ---------- API publique (identique à l’ancienne) ----------
def list_saved() -> list[dict[str, Any]]:
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

    out: list[dict[str, Any]] = []
    for r in rows or []:
        payload = r.get("payload") or {}
        meta = payload.get("_meta", {})
        ts = meta.get("ts")
        if not ts and r.get("created_at"):
            ca = r["created_at"]
            try:
                ts = ca.isoformat() if hasattr(ca, "isoformat") else str(ca)
            except (ValueError, TypeError):
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


def save_snapshot(name: str, sp: dict[str, Any]) -> tuple[bool, str]:
    """Crée / remplace une proposition (MAX_SLOTS par tenant basé sur les NOMS distincts)."""
    name = (name or "").strip()
    if not name:
        return False, "Nom vide."

    t_id = _tenant_id()
    u_id = _system_user_id(t_id)

    # construit le payload applicatif + meta horodatée
    payload = _encode_sp(sp)
    ts = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
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

    # Insert atomique avec vérification de la limite MAX_SLOTS (pas de race condition)
    inserted = run_sql(
        """
        INSERT INTO production_proposals (tenant_id, created_by, payload, status, created_at, updated_at)
        SELECT :t, :u, CAST(:p AS JSONB), 'draft', now(), now()
        WHERE (
            SELECT COUNT(DISTINCT payload->'_meta'->>'name')
            FROM production_proposals
            WHERE tenant_id = :t
        ) < :max_slots
        RETURNING id
        """,
        {"t": t_id, "u": u_id, "p": json.dumps(payload), "max_slots": MAX_SLOTS},
    )
    if not inserted:
        return False, f"Limite atteinte ({MAX_SLOTS}). Supprime ou renomme une entrée."

    return True, "Proposition enregistrée."


def load_snapshot(name: str) -> dict[str, Any] | None:
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


def rename_snapshot(old: str, new: str) -> tuple[bool, str]:
    """Renomme une proposition de facon atomique (pas de race condition TOCTOU)."""
    new = (new or "").strip()
    if not new:
        return False, "Nouveau nom vide."
    if old == new:
        return True, "Aucun changement."
    t_id = _tenant_id()

    # UPDATE atomique : ne modifie QUE si le nouveau nom n'existe pas deja
    try:
        rows = run_sql(
            """
            UPDATE production_proposals
            SET payload = jsonb_set(payload, '{_meta,name}', to_jsonb(:new_name::text), true),
                updated_at = NOW()
            WHERE tenant_id = :t
              AND payload->'_meta'->>'name' = :old_name
              AND NOT EXISTS (
                  SELECT 1 FROM production_proposals pp2
                  WHERE pp2.tenant_id = :t
                    AND pp2.payload->'_meta'->>'name' = :new_name
              )
            RETURNING id
            """,
            {"t": t_id, "old_name": old, "new_name": new},
        )
    except Exception as exc:
        # L'index unique idx_pp_unique_name_per_tenant intercepte les race conditions
        from sqlalchemy.exc import IntegrityError
        cause = getattr(exc, "__cause__", exc)
        if isinstance(exc, IntegrityError) or isinstance(cause, IntegrityError):
            return False, "Ce nom existe déjà."
        raise
    if not rows:
        # Distinguer : nom deja pris vs entree introuvable
        exists = run_sql(
            """
            SELECT 1 FROM production_proposals
            WHERE tenant_id = :t AND payload->'_meta'->>'name' = :new_name
            LIMIT 1
            """,
            {"t": t_id, "new_name": new},
        )
        if exists:
            return False, "Ce nom existe deja."
        return False, "Entree introuvable."
    return True, "Renommee."
