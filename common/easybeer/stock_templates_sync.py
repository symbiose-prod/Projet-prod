"""
common/easybeer/stock_templates_sync.py
========================================
Synchronisation des **templates "stock produit fini"** EasyBeer vers la
table DB ``eb_stock_product_templates``.

**Pourquoi cette synchro ?**

Pour reconstruire le payload ``POST /brassin/mise-en-bouteille`` conforme à
ce qu'EB attend (cf. ``docs/easybeer-write-payloads/``), notre backend doit
savoir, pour chaque combinaison ``(idProduit, contenance, marque)`` :

- Quel ``idStockBouteille`` (empty bottle stock) débiter
- Quels ``elementsConditionnement`` (capsules, cartons, étiquettes) débiter
- Quel PCB (lot_quantite) pour calculer les bouteilles depuis les cartons

Tout ça vit dans les "stock produit" EB, indexés par ``codeArticle`` (ex.
``SK-KDF-33-ORI``, ``NIKO-KDF-75-GIN``). Notre table en est un snapshot.

**Pipeline de sync** :

1. ``POST /stock/produits`` avec ``{"idBrasserie": X}`` → liste hiérarchique
   des stocks produits (``consolidationsFilles[]`` à 2 niveaux).
2. Pour chaque ``id`` trouvé : ``GET /stock/produit/edition/{id}`` (avec
   le cache L1 existant dans ``stocks.py``) → détail complet.
3. UPSERT dans ``eb_stock_product_templates``.

Lancement :
- Page admin : bouton "Resync from EB" → appelle ``sync_all_templates()``
- (Plus tard) cron quotidien

Idempotent : on peut le lancer plusieurs fois sans casser quoi que ce soit.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from common._session import current_tenant_id
from db.conn import run_sql

from . import stocks as _stocks
from ._client import BASE, EasyBeerError, _auth, _check_response, _safe_json, _safe_list, get_session

_log = logging.getLogger("ferment.easybeer.stock_templates")


# ─── Helpers ──────────────────────────────────────────────────────────────


def _normalize_template(detail: dict[str, Any]) -> dict[str, Any] | None:
    """Extrait les champs DB depuis la réponse GET /stock/produit/edition/{id}.

    Retourne None si ``codeArticle`` ou ``idProduit`` manquant — on ne stocke
    pas les entrées sans identifiant exploitable.
    """
    code_article = detail.get("codeArticle")
    produit = detail.get("produit") or {}
    id_produit = produit.get("idProduit")
    if not code_article or not id_produit:
        return None

    contenant = detail.get("contenant") or {}
    lot = detail.get("lot") or {}

    # elementsConditionnement : on garde une forme épurée pour limiter la
    # taille du JSONB et faciliter le requêtage.
    elements: list[dict[str, Any]] = []
    for elem in detail.get("elementsConditionnement") or []:
        mp = elem.get("elementMatierePremiere") or {}
        id_mp = mp.get("idMatierePremiere")
        if not id_mp:
            continue
        elements.append({
            "idMatierePremiere": int(id_mp),
            "libelle": str(mp.get("libelle") or ""),
            "code": str(mp.get("code") or ""),
            "type": (mp.get("type") or {}).get("code"),  # ex CONDITIONNEMENT_CAPSULE
            "quantite": float(elem.get("quantite") or 0),
        })

    return {
        "id_stock_produit": int(detail["idStockProduit"]),
        "code_article": str(code_article),
        "id_produit": int(id_produit),
        "produit_libelle": str(produit.get("libelle") or produit.get("nom") or ""),
        "id_contenant": int(contenant["idContenant"]) if contenant.get("idContenant") else None,
        "contenant_libelle": str(contenant.get("libelleAvecContenance") or contenant.get("libelle") or ""),
        "contenance": float(contenant["contenance"]) if contenant.get("contenance") else None,
        "id_lot": int(lot["idLot"]) if lot.get("idLot") else None,
        "lot_libelle": str(lot.get("libelle") or ""),
        "lot_quantite": int(lot["quantite"]) if lot.get("quantite") else None,
        "elements_conditionnement": elements,
        "raw_data": detail,
    }


def _upsert_template(tenant_id: str, template: dict[str, Any]) -> None:
    """UPSERT idempotent dans eb_stock_product_templates."""
    run_sql(
        """
        INSERT INTO eb_stock_product_templates
            (tenant_id, id_stock_produit, code_article, id_produit,
             produit_libelle, id_contenant, contenant_libelle, contenance,
             id_lot, lot_libelle, lot_quantite,
             elements_conditionnement, raw_data, synced_at)
        VALUES
            (:tid, :isp, :ca, :ip,
             :pl, :ic, :cl, :ct,
             :il, :ll, :lq,
             CAST(:ec AS jsonb), CAST(:rd AS jsonb), now())
        ON CONFLICT (tenant_id, id_stock_produit) DO UPDATE
            SET code_article             = EXCLUDED.code_article,
                id_produit               = EXCLUDED.id_produit,
                produit_libelle          = EXCLUDED.produit_libelle,
                id_contenant             = EXCLUDED.id_contenant,
                contenant_libelle        = EXCLUDED.contenant_libelle,
                contenance               = EXCLUDED.contenance,
                id_lot                   = EXCLUDED.id_lot,
                lot_libelle              = EXCLUDED.lot_libelle,
                lot_quantite             = EXCLUDED.lot_quantite,
                elements_conditionnement = EXCLUDED.elements_conditionnement,
                raw_data                 = EXCLUDED.raw_data,
                synced_at                = now()
        """,
        {
            "tid": tenant_id,
            "isp": template["id_stock_produit"],
            "ca": template["code_article"],
            "ip": template["id_produit"],
            "pl": template["produit_libelle"],
            "ic": template["id_contenant"],
            "cl": template["contenant_libelle"],
            "ct": template["contenance"],
            "il": template["id_lot"],
            "ll": template["lot_libelle"],
            "lq": template["lot_quantite"],
            "ec": json.dumps(template["elements_conditionnement"], default=str, ensure_ascii=False),
            "rd": json.dumps(template["raw_data"], default=str, ensure_ascii=False),
        },
    )


def _list_stock_produit_ids() -> list[int]:
    """``POST /stock/produits`` → liste plate des ``idStockProduit``.

    Le payload retourné est hiérarchique (un niveau par produit, un
    sous-niveau par stock fils par format). On aplatit pour ne garder que
    les feuilles avec un ``id``.
    """
    payload = {"idBrasserie": int(os.environ.get("EASYBEER_ID_BRASSERIE", "0"))}
    r = get_session().post(
        f"{BASE}/stock/produits",
        json=payload,
        auth=_auth(),
        timeout=30,
    )
    _check_response(r, "stock/produits")
    data = _safe_json(r, "stock/produits")

    ids: list[int] = []
    for prod in _safe_list(data, "consolidationsFilles", "stock/produits"):
        for conso in _safe_list(prod, "consolidationsFilles", "stock/produits"):
            sid = conso.get("id")
            if isinstance(sid, int):
                ids.append(sid)
    return ids


# ─── API publique ─────────────────────────────────────────────────────────


def sync_all_templates(tenant_id: str | None = None) -> dict[str, int]:
    """Sync tous les templates stock produit EB → DB.

    Args:
        tenant_id: scope multi-tenant. Si ``None``, lit ``current_tenant_id()``.

    Returns:
        ``{"total": N, "upserted": M, "skipped": K, "errors": E}`` pour
        l'observabilité depuis la page admin et les logs.

    L'opération est best-effort : si un ``GET edition/{id}`` échoue
    (rate-limit, 5xx ponctuel), on log l'erreur et on continue avec les
    autres entries. Une seconde passe de sync les rattrapera.
    """
    tid = tenant_id or current_tenant_id()
    if not tid:
        raise RuntimeError("sync_all_templates: tenant_id manquant (pas de current_tenant_id)")

    started = time.monotonic()
    stats = {"total": 0, "upserted": 0, "skipped": 0, "errors": 0}

    try:
        ids = _list_stock_produit_ids()
    except EasyBeerError as exc:
        _log.error("sync_all_templates: list_stock_produit_ids failed: %s", exc)
        raise

    stats["total"] = len(ids)
    _log.info("sync_all_templates: %d idStockProduit à synchroniser (tenant=%s)", len(ids), tid)

    for sid in ids:
        try:
            detail = _stocks.get_stock_produit_detail(sid)
            template = _normalize_template(detail)
            if template is None:
                stats["skipped"] += 1
                _log.debug("sync: skip id=%s (codeArticle ou idProduit manquant)", sid)
                continue
            _upsert_template(tid, template)
            stats["upserted"] += 1
        except (EasyBeerError, Exception) as exc:  # noqa: BLE001
            stats["errors"] += 1
            _log.warning("sync_all_templates: id=%s échec: %s", sid, exc)

    duration_s = time.monotonic() - started
    _log.info(
        "sync_all_templates terminée en %.1fs : %s",
        duration_s, stats,
    )
    return stats


def list_synced_templates(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Lecture simple de la table (pour page admin).

    Trie par codeArticle pour stabilité d'affichage.
    """
    tid = tenant_id or current_tenant_id()
    if not tid:
        return []
    rows = run_sql(
        """
        SELECT id_stock_produit, code_article, id_produit, produit_libelle,
               id_contenant, contenant_libelle, contenance,
               id_lot, lot_libelle, lot_quantite,
               jsonb_array_length(elements_conditionnement) AS n_elements,
               synced_at
          FROM eb_stock_product_templates
         WHERE tenant_id = :tid
         ORDER BY code_article
        """,
        {"tid": tid},
    ) or []
    return [dict(r) for r in rows]


def find_template(
    *,
    tenant_id: str,
    id_produit: int,
    contenance: float,
    lot_quantite: int,
) -> dict[str, Any] | None:
    """Lookup unique pour le resolver mise-en-bouteille.

    Args:
        tenant_id: scope.
        id_produit: produit EB (ex 42397 pour Kéfir de fruits Original).
        contenance: 0.33 ou 0.75.
        lot_quantite: PCB du conditionnement (6 pour Carton de 6, 12 pour Carton de 12, etc.)

    Returns:
        Le template (dict) si unique, sinon None. Si plusieurs templates
        matchent (cas SAFT vs Verralia pour 75cl), retourne aussi None et
        le caller doit désambiguïser avec un signal supplémentaire (marque
        depuis la fiche → ``find_template_by_marque``).
    """
    rows = run_sql(
        """
        SELECT id_stock_produit, code_article, id_produit, produit_libelle,
               id_contenant, contenant_libelle, contenance,
               id_lot, lot_libelle, lot_quantite,
               elements_conditionnement, synced_at
          FROM eb_stock_product_templates
         WHERE tenant_id   = :tid
           AND id_produit  = :ip
           AND ABS(contenance - :ct) < 0.001
           AND lot_quantite = :lq
         LIMIT 2
        """,
        {"tid": tenant_id, "ip": id_produit, "ct": contenance, "lq": lot_quantite},
    ) or []
    if len(rows) == 1:
        return dict(rows[0])
    return None  # 0 match ou ambigu


def find_template_by_code_article(
    *,
    tenant_id: str,
    code_article: str,
) -> dict[str, Any] | None:
    """Lookup par codeArticle (ex 'SK-KDF-75-ORI'). Pour pages admin et debug."""
    rows = run_sql(
        """
        SELECT id_stock_produit, code_article, id_produit, produit_libelle,
               id_contenant, contenant_libelle, contenance,
               id_lot, lot_libelle, lot_quantite,
               elements_conditionnement, raw_data, synced_at
          FROM eb_stock_product_templates
         WHERE tenant_id    = :tid
           AND code_article = :ca
         LIMIT 1
        """,
        {"tid": tenant_id, "ca": code_article},
    ) or []
    return dict(rows[0]) if rows else None
