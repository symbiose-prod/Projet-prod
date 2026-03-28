"""
common/client_cache.py
======================
Cache nocturne des clients EasyBeer : données de base + tags.

Le job ``sync_clients()`` récupère la liste paginée des clients, puis
fetch le détail de chacun pour extraire les tags (non disponibles dans
la liste). Les résultats sont stockés dans ``client_cache`` et les tags
distincts dans ``client_tags_cache``.

Durée estimée : ~5 min pour 300 clients (rate-limit 1 req/s).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from db.conn import run_sql

_log = logging.getLogger("ferment.client_cache")


# ─── Fetch depuis EasyBeer ───────────────────────────────────────────────────

def _fetch_client_list() -> list[dict[str, Any]]:
    """Récupère tous les clients via POST /parametres/client/liste (paginé)."""
    from common.easybeer._client import (
        BASE,
        _auth,
        _check_response,
        _safe_json,
        get_session,
        is_rate_limited,
    )

    all_clients: list[dict[str, Any]] = []
    page = 1
    max_pages = 50

    while page <= max_pages:
        if is_rate_limited() > 0:
            _log.warning("Rate-limit actif, arrêt pagination clients (page %d)", page)
            break

        payload = {
            "recherche": "",
            "tags": "",
            "idsClientsTypes": [],
            "idsClientsTournees": [],
            "supprime": False,
            "idsClients": [],
        }
        r = get_session().post(
            f"{BASE}/parametres/client/liste",
            params={
                "numeroPage": page,
                "nombreParPage": 200,
                "colonneTri": "nom",
                "mode": "ASC",
            },
            json=payload,
            auth=_auth(),
            timeout=30,
        )
        _check_response(r, "parametres/client/liste")
        data = _safe_json(r, "parametres/client/liste")

        liste = data.get("liste") or []
        all_clients.extend(liste)
        total_pages = data.get("totalPages", 1)

        if page >= total_pages or not liste:
            break
        page += 1

    _log.info("Client list: %d clients récupérés (%d pages)", len(all_clients), page)
    return all_clients


def _fetch_client_detail(id_client: int) -> dict[str, Any] | None:
    """GET /parametres/client/edition/{id} → détail avec tags."""
    from common.easybeer._client import BASE, _auth, get_session, is_rate_limited

    if is_rate_limited() > 0:
        return None

    r = get_session().get(
        f"{BASE}/parametres/client/edition/{id_client}",
        auth=_auth(),
        timeout=30,
    )
    if not r.ok:
        _log.debug("Client detail %d: HTTP %d", id_client, r.status_code)
        return None
    return r.json()


# ─── Sync complet ────────────────────────────────────────────────────────────

def sync_clients(tenant_id: str) -> dict[str, int]:
    """Sync complète des clients EasyBeer → client_cache + client_tags_cache.

    1. Fetch liste paginée (noms, types, tournées)
    2. Fetch détail de chaque client (tags)
    3. Upsert dans client_cache
    4. Reconstruire client_tags_cache

    Returns ``{"clients": N, "tags": M}``.
    """
    _log.info("Début sync clients pour tenant %s", tenant_id)
    start = time.monotonic()

    # ── 1. Liste paginée ──
    clients = _fetch_client_list()
    if not clients:
        _log.warning("Aucun client récupéré depuis EasyBeer")
        return {"clients": 0, "tags": 0}

    # ── 2. Fetch détails pour les tags ──
    # On construit un dict id_client → tags
    tags_by_client: dict[int, list[str]] = {}
    fetched = 0
    skipped = 0

    # Récupérer les types clients pour la hiérarchie parent
    type_parents = _fetch_type_parents()

    for i, c in enumerate(clients):
        id_client = c.get("idClient")
        if not id_client:
            continue

        detail = _fetch_client_detail(id_client)
        if detail is None:
            skipped += 1
            if skipped > 5:
                _log.warning("Trop de clients skippés (rate-limit?), arrêt fetch détails")
                break
            continue

        tags = detail.get("tags") or []
        if isinstance(tags, list):
            tags_by_client[id_client] = [str(t) for t in tags if t]
        elif isinstance(tags, str) and tags.strip():
            tags_by_client[id_client] = [t.strip() for t in tags.split(",") if t.strip()]

        fetched += 1
        if fetched % 50 == 0:
            _log.info("Sync clients: %d/%d détails fetchés", fetched, len(clients))

    _log.info("Détails fetchés: %d/%d (skipped: %d)", fetched, len(clients), skipped)

    # ── 3. Upsert client_cache ──
    upserted = 0
    for c in clients:
        id_client = c.get("idClient")
        if not id_client:
            continue

        nom = (c.get("nom") or "").strip()
        numero = (c.get("numero") or "").strip()
        type_obj = c.get("type") or {}
        type_libelle = (type_obj.get("libelle") or "").strip()
        type_parent = type_parents.get(type_libelle, "")
        tournee_obj = c.get("tournee") or {}
        tournee = (tournee_obj.get("libelle") or "").strip() if isinstance(tournee_obj, dict) else ""
        actif = c.get("actif", True)
        tags = tags_by_client.get(id_client, [])

        run_sql(
            """
            INSERT INTO client_cache
                (tenant_id, id_client, nom, numero, type_libelle,
                 type_parent, tournee, tags, actif, synced_at)
            VALUES (:tid, :idc, :nom, :num, :tl, :tp, :tour, :tags, :actif, now())
            ON CONFLICT (tenant_id, id_client) DO UPDATE SET
                nom = EXCLUDED.nom,
                numero = EXCLUDED.numero,
                type_libelle = EXCLUDED.type_libelle,
                type_parent = EXCLUDED.type_parent,
                tournee = EXCLUDED.tournee,
                tags = EXCLUDED.tags,
                actif = EXCLUDED.actif,
                synced_at = now()
            """,
            {
                "tid": tenant_id,
                "idc": id_client,
                "nom": nom,
                "num": numero,
                "tl": type_libelle,
                "tp": type_parent,
                "tour": tournee,
                "tags": tags,
                "actif": actif,
            },
        )
        upserted += 1

    # ── 4. Reconstruire client_tags_cache ──
    # Collecter tous les tags uniques avec leur count
    tag_counts: dict[str, int] = {}
    for tags_list in tags_by_client.values():
        for tag in tags_list:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # Vider et reconstruire
    run_sql("DELETE FROM client_tags_cache WHERE tenant_id = :tid", {"tid": tenant_id})
    for tag, count in sorted(tag_counts.items()):
        run_sql(
            """
            INSERT INTO client_tags_cache (tenant_id, tag, client_count, synced_at)
            VALUES (:tid, :tag, :cnt, now())
            """,
            {"tid": tenant_id, "tag": tag, "cnt": count},
        )

    elapsed = time.monotonic() - start
    _log.info(
        "Sync clients terminée: %d clients, %d tags uniques (%.0fs)",
        upserted, len(tag_counts), elapsed,
    )
    return {"clients": upserted, "tags": len(tag_counts)}


def _fetch_type_parents() -> dict[str, str]:
    """Fetch la hiérarchie des types clients → {type_libelle: parent_libelle}."""
    from common.easybeer._client import BASE, _auth, get_session

    try:
        r = get_session().get(
            f"{BASE}/parametres/client/type",
            auth=_auth(),
            timeout=15,
        )
        if not r.ok:
            return {}
        data = r.json()
        if not isinstance(data, list):
            return {}
        return {
            t["libelle"]: t.get("libelleParent", "")
            for t in data
            if t.get("libelle")
        }
    except Exception:
        _log.warning("Impossible de charger les types clients", exc_info=True)
        return {}


# ─── Lecture du cache ────────────────────────────────────────────────────────

def get_all_tags(tenant_id: str) -> list[dict[str, Any]]:
    """Retourne les tags distincts avec leur nombre de clients [{tag, client_count}]."""
    return run_sql(
        """
        SELECT tag, client_count
        FROM client_tags_cache
        WHERE tenant_id = :tid
        ORDER BY tag
        """,
        {"tid": tenant_id},
    ) or []


def get_all_tournees(tenant_id: str) -> list[str]:
    """Retourne les tournées distinctes."""
    rows = run_sql(
        """
        SELECT DISTINCT tournee
        FROM client_cache
        WHERE tenant_id = :tid AND tournee != '' AND actif = true
        ORDER BY tournee
        """,
        {"tid": tenant_id},
    )
    return [r["tournee"] for r in (rows or [])]


def get_all_types(tenant_id: str) -> list[dict[str, str]]:
    """Retourne les types clients distincts avec leur parent [{type_libelle, type_parent}]."""
    return run_sql(
        """
        SELECT DISTINCT type_libelle, type_parent
        FROM client_cache
        WHERE tenant_id = :tid AND type_libelle != '' AND actif = true
        ORDER BY type_parent, type_libelle
        """,
        {"tid": tenant_id},
    ) or []


def get_last_sync(tenant_id: str) -> str | None:
    """Retourne la date de dernière sync (ISO) ou None."""
    rows = run_sql(
        "SELECT MAX(synced_at) AS ts FROM client_cache WHERE tenant_id = :tid",
        {"tid": tenant_id},
    )
    if rows and rows[0].get("ts"):
        ts = rows[0]["ts"]
        return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    return None
