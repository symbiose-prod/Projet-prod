from __future__ import annotations

import asyncio
import datetime as _dt_local
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from common.mobile_v1 import (
    _forbidden,
    _resolve_mobile_user,
    _unauthorized,
)

_log = logging.getLogger("ferment.mobile_v1")


# ─── Fiches de production (admin only, beta) ───────────────────────────────

async def _v1_create_production_sheet(request: Request):
    """Crée une fiche de production (status ``'draft'``). ADMIN only.

    Body JSON (tous champs optionnels) :
      ``{"brassin_id": "...", "produit": "...", "cuve": "...",
         "ddm": "YYYY-MM-DD", "lot": "...", "data": {...}}``

    Retour 200 : ``{"id": "uuid"}``.
    Retour 400 : body invalide (ex: ddm mal formée).
    Retour 403 : non admin.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}

    ddm_str = (body.get("ddm") or "").strip() if body.get("ddm") else ""
    ddm: _dt_local.date | None = None
    if ddm_str:
        try:
            ddm = _dt_local.date.fromisoformat(ddm_str[:10])
        except ValueError:
            return JSONResponse(
                {"error": "Invalid ddm format (expected YYYY-MM-DD)"},
                status_code=400,
            )

    data_value = body.get("data") or {}
    if not isinstance(data_value, dict):
        return JSONResponse(
            {"error": "'data' must be an object"}, status_code=400,
        )

    from common.services.production_sheet_service import create_sheet

    try:
        sheet_id = await asyncio.to_thread(
            create_sheet,
            user["tenant_id"],
            user_id=user["id"],
            brassin_id=(body.get("brassin_id") or None),
            produit=str(body.get("produit") or ""),
            cuve=str(body.get("cuve") or ""),
            ddm=ddm,
            lot=str(body.get("lot") or ""),
            data=data_value,
        )
    except Exception:
        _log.exception("Échec création fiche production (mobile)")
        return JSONResponse(
            {"error": "Failed to create production sheet"}, status_code=500,
        )

    return JSONResponse({"id": sheet_id})


async def _v1_list_production_sheets(request: Request):
    """Liste paginée des fiches de production. ADMIN only.

    Query : ``?limit=20&offset=0&status=draft|completed`` (status optionnel).

    Retour 200 :
      ``{"sheets": [{id, brassin_id, produit, cuve, ddm, lot, status,
                     created_at, updated_at, finalized_at,
                     created_by_email}, ...],
         "total": N, "limit": 20, "offset": 0}``
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")

    params = request.query_params
    try:
        limit = int(params.get("limit") or "20")
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 100))
    try:
        offset = int(params.get("offset") or "0")
    except ValueError:
        offset = 0
    offset = max(0, offset)
    status = (params.get("status") or "").strip().lower() or None

    from common.services.production_sheet_service import (
        count_sheets,
        list_sheets,
    )

    summaries, total = await asyncio.gather(
        asyncio.to_thread(
            list_sheets, user["tenant_id"],
            limit=limit, offset=offset, status=status,
        ),
        asyncio.to_thread(count_sheets, user["tenant_id"], status=status),
    )
    payload = [
        {
            "id": s.id,
            "brassin_id": s.brassin_id,
            "produit": s.produit,
            "cuve": s.cuve,
            "ddm": s.ddm.isoformat() if s.ddm else None,
            "lot": s.lot,
            "status": s.status,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            "finalized_at": (
                s.finalized_at.isoformat() if s.finalized_at else None
            ),
            "created_by_email": s.created_by_email or "",
        }
        for s in summaries
    ]
    return JSONResponse({
        "sheets": payload,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


async def _v1_admin_brassins_en_cours(request: Request):
    """Liste les brassins EasyBeer en cours (+ archives récentes). ADMIN only.

    Réutilise ``ramasse_service.load_active_brassins()`` qui gère le fallback
    EasyBeer down (renvoie ``(brassins, errors)`` au lieu de raise).

    Retour 200 :
      ``{"brassins": [{id_brassin, nom, produit_libelle, id_produit, volume,
                       is_archive}, ...],
         "errors": ["..."]}``

    ``errors`` est la liste des messages user-friendly à afficher si l'app
    veut prévenir d'un fetch partiel (ex: EasyBeer rate-limited).
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")

    from common.services.ramasse_service import load_active_brassins

    brassins, errors = await asyncio.to_thread(load_active_brassins, 3)
    payload = [
        {
            "id_brassin": b.id_brassin,
            "nom": b.nom,
            "produit_libelle": b.produit_libelle,
            "id_produit": b.id_produit,
            "volume": b.volume,
            "is_archive": b.is_archive,
        }
        for b in brassins
    ]
    return JSONResponse({"brassins": payload, "errors": errors})


async def _v1_admin_conditionnement_by_lot(request: Request):
    """Agrège les palettes étiquetées pour un lot en lignes (fmt, marque). ADMIN.

    Query : ``?lot=15052027`` (obligatoire).

    Sert au pré-remplissage de la section "Conditionnement réel" d'une fiche
    de production : on n'a pas à ressaisir manuellement les cartons/palettes
    déjà étiquetés et scannés, on les agrège depuis ``sscc_log`` (source
    de vérité).

    Retour 200 :
      ``{"lot": "...",
         "items": [{fmt, marque, designation, cartons, palettes}, ...],
         "total_cartons": N, "total_palettes": M}``

    Retour 400 : lot absent ou vide.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")

    lot = (request.query_params.get("lot") or "").strip()
    if not lot:
        return JSONResponse({"error": "Missing 'lot' query param"}, status_code=400)
    # Filtre optionnel par libellé produit : permet de désambiguïser quand
    # 2 brassins partagent la même DDM (= même lot). iOS passe sheet.produit
    # à la création/refresh de la section Conditionnement.
    produit_filter = (request.query_params.get("produit") or "").strip() or None

    from common.services.production_sheet_service import (
        compute_real_conditionnement_by_lot,
    )

    result = await asyncio.to_thread(
        compute_real_conditionnement_by_lot,
        user["tenant_id"], lot,
        produit_filter=produit_filter,
    )
    return JSONResponse({
        "lot": result.lot,
        "items": [
            {
                "fmt": i.fmt,
                "marque": i.marque,
                "designation": i.designation,
                "cartons": i.cartons,
                "palettes": i.palettes,
            }
            for i in result.items
        ],
        "total_cartons": result.total_cartons,
        "total_palettes": result.total_palettes,
    })


def _serialize_sheet_detail(s) -> dict:
    """Sérialise ProductionSheetDetail en dict JSON pour le mobile.

    ``image_url`` : visuel produit servi par le VPS (mapping
    ``assets/image_map.csv`` — même mécanisme que les étiquettes), résolu
    par correspondance floue sur le libellé produit. ``None`` si pas de
    visuel pour ce produit.
    """
    from common.services.etiquette_palette_service import get_product_image_url

    return {
        "id": s.id,
        "brassin_id": s.brassin_id,
        "produit": s.produit,
        "cuve": s.cuve,
        "ddm": s.ddm.isoformat() if s.ddm else None,
        "lot": s.lot,
        "status": s.status,
        "version": s.version,
        "data": s.data,
        "image_url": get_product_image_url(s.produit),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "finalized_at": (
            s.finalized_at.isoformat() if s.finalized_at else None
        ),
        "created_by_email": s.created_by_email or "",
    }


async def _v1_get_production_sheet(sheet_id: str, request: Request):
    """Détail complet d'une fiche (avec ``data`` JSONB). ADMIN only.

    Retour 200 : voir ``_serialize_sheet_detail``.
    Retour 404 : introuvable ou hors tenant.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")

    from common.services.production_sheet_service import get_sheet

    detail = await asyncio.to_thread(get_sheet, user["tenant_id"], sheet_id)
    if detail is None:
        return JSONResponse({"error": "Sheet not found"}, status_code=404)
    return JSONResponse(_serialize_sheet_detail(detail))


async def _v1_patch_production_sheet(sheet_id: str, request: Request):
    """Mise à jour partielle d'une fiche (auto-save iOS). ADMIN only.

    Body JSON : tous champs optionnels. Seuls les champs fournis sont mis
    à jour (sémantique PATCH).

    Exemple : ``{"produit": "K. Mangue", "data": {"fermentation": {...}}}``.

    La fiche doit être en status ``'draft'``. Pas de modif si déjà
    ``'completed'`` → 404 (la fiche reste accessible en lecture mais
    figée).

    Optimistic locking : le client peut envoyer ``version`` (la valeur lue
    au dernier GET). Si la fiche a été modifiée entre temps par un autre
    client, le serveur renvoie 409 sans rien écraser. ``version`` absent =
    comportement legacy last-write-wins (clients iOS pas encore à jour).

    Retour 200 : ``{"ok": true}`` + fiche complète mise à jour (mêmes
    champs que GET) pour permettre au client de re-synchroniser sans
    requête supplémentaire.
    Retour 400 : body invalide (ex: ddm mal formée, version non entière).
    Retour 404 : fiche introuvable, hors tenant, ou déjà finalisée.
    Retour 409 : conflit de version — la réponse inclut ``sheet`` (l'état
    serveur courant) pour que le client puisse re-synchroniser.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    body = body or {}

    # Parse les champs présents — on n'ajoute aux kwargs QUE les champs
    # fournis. Le service utilise un sentinel `_UNSET` par défaut pour
    # distinguer "non fourni" (skip dans SQL) de "fourni avec None" (NULL).
    from common.services.production_sheet_service import get_sheet, patch_sheet

    kwargs: dict = {}
    if "produit" in body:
        kwargs["produit"] = body.get("produit") or ""
    if "cuve" in body:
        kwargs["cuve"] = body.get("cuve") or ""
    if "lot" in body:
        kwargs["lot"] = body.get("lot") or ""
    if "brassin_id" in body:
        kwargs["brassin_id"] = body.get("brassin_id") or None
    if "ddm" in body:
        ddm_value = body.get("ddm")
        if ddm_value in (None, ""):
            kwargs["ddm"] = None
        else:
            try:
                kwargs["ddm"] = _dt_local.date.fromisoformat(str(ddm_value)[:10])
            except ValueError:
                return JSONResponse(
                    {"error": "Invalid ddm format (expected YYYY-MM-DD)"},
                    status_code=400,
                )
    if "data" in body:
        if not isinstance(body["data"], dict):
            return JSONResponse(
                {"error": "'data' must be an object"}, status_code=400,
            )
        kwargs["data"] = body["data"]

    # Optimistic locking : version attendue (optionnelle). Pas un champ
    # patchable — on la passe à part comme expected_version.
    expected_version: int | None = None
    if "version" in body and body.get("version") is not None:
        try:
            expected_version = int(body["version"])
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "'version' must be an integer"}, status_code=400,
            )

    if not kwargs:
        # Rien à patcher — on renvoie la fiche actuelle (idempotent friendly)
        detail = await asyncio.to_thread(get_sheet, user["tenant_id"], sheet_id)
        if detail is None:
            return JSONResponse({"error": "Sheet not found"}, status_code=404)
        return JSONResponse({"ok": True, "sheet": _serialize_sheet_detail(detail)})

    result = await asyncio.to_thread(
        patch_sheet, user["tenant_id"], sheet_id,
        expected_version=expected_version, **kwargs,
    )

    if result == "conflict":
        # La fiche a changé depuis le dernier GET du client. On renvoie
        # l'état serveur courant pour qu'il puisse re-synchroniser.
        detail = await asyncio.to_thread(get_sheet, user["tenant_id"], sheet_id)
        return JSONResponse(
            {
                "error": "Version conflict — sheet modified by another client",
                "sheet": _serialize_sheet_detail(detail) if detail else None,
            },
            status_code=409,
        )

    if result != "ok":
        # "not_found" ou "no_changes" (ce dernier improbable ici : kwargs
        # est non vide à ce stade).
        return JSONResponse(
            {"error": "Sheet not found, not editable, or unchanged"},
            status_code=404,
        )

    detail = await asyncio.to_thread(get_sheet, user["tenant_id"], sheet_id)
    return JSONResponse({
        "ok": True,
        "sheet": _serialize_sheet_detail(detail) if detail else None,
    })


async def _v1_production_overview(request: Request):
    """Liste des brassins EB actifs + fiches draft associées. ADMIN only.

    Vue principale du tab Production iOS : pour chaque brassin EasyBeer en
    cours, on indique s'il a déjà une fiche démarrée (status draft) ou non.

    Retour 200 :
      ``{"brassins": [{brassin: {id_brassin, nom, produit_libelle, ...},
                       sheet: {id, status, updated_at, ...} | null}, ...],
         "easybeer_errors": ["..."]}``

    L'app iOS affiche cette liste comme la "liste des productions en cours" :
    chaque brassin EB est une ligne. Tap sur un brassin : si ``sheet=null``,
    POST create + nav ; si sheet existe, nav direct vers son détail.

    Le destinataire du cache : ``eb_cache.brassins_en_cours`` (TTL 5 min
    via ``load_active_brassins``). Refresh automatique.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")

    from common.services.production_sheet_service import (
        find_sheets_by_brassin_ids,
    )
    from common.services.ramasse_service import load_active_brassins

    brassins, eb_errors = await asyncio.to_thread(load_active_brassins, 0)
    # 0 archives — on veut juste les brassins en cours (l'archive vient des
    # fiches `completed` côté DB, pas des brassins archivés EB).

    brassin_ids = [str(b.id_brassin) for b in brassins if b.id_brassin]
    sheets_by_bid = await asyncio.to_thread(
        find_sheets_by_brassin_ids, user["tenant_id"], brassin_ids,
    )

    items: list[dict] = []
    for b in brassins:
        bid = str(b.id_brassin) if b.id_brassin else ""
        sheet_summary = sheets_by_bid.get(bid)
        sheet_payload: dict | None = None
        if sheet_summary is not None:
            sheet_payload = {
                "id": sheet_summary.id,
                "status": sheet_summary.status,
                "produit": sheet_summary.produit,
                "cuve": sheet_summary.cuve,
                "ddm": sheet_summary.ddm.isoformat() if sheet_summary.ddm else None,
                "lot": sheet_summary.lot,
                "created_at": (
                    sheet_summary.created_at.isoformat()
                    if sheet_summary.created_at else None
                ),
                "updated_at": (
                    sheet_summary.updated_at.isoformat()
                    if sheet_summary.updated_at else None
                ),
            }
        items.append({
            "brassin": {
                "id_brassin": b.id_brassin,
                "nom": b.nom,
                "produit_libelle": b.produit_libelle,
                "id_produit": b.id_produit,
                "volume": b.volume,
                "is_archive": b.is_archive,
            },
            "sheet": sheet_payload,
        })
    return JSONResponse({
        "brassins": items,
        "easybeer_errors": eb_errors,
    })


async def _v1_finalize_production_sheet(sheet_id: str, request: Request):
    """Finalise une fiche : génère PDF + status 'completed'. ADMIN only.

    Retour 200 : PDF binaire (Content-Type: application/pdf). Headers meta :
      - ``X-Sheet-Id``: UUID de la fiche
      - ``X-Sheet-Finalized-At``: ISO 8601 timestamp finalisation
      - ``Content-Disposition``: attachment avec filename construit depuis
        produit + lot pour reconnaissance facile dans le téléchargement.

    Retour 404 : fiche introuvable, hors tenant, ou déjà finalisée.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")

    from common.services.production_sheet_service import finalize_sheet

    try:
        detail, pdf_bytes = await asyncio.to_thread(
            finalize_sheet,
            user["tenant_id"], sheet_id,
            user_email=user.get("email") or "",
        )
    except ValueError as exc:
        msg = str(exc)
        status = 409 if "already" in msg.lower() else 404
        return JSONResponse({"error": msg}, status_code=status)
    except Exception:
        _log.exception("Échec finalize production sheet id=%s", sheet_id)
        return JSONResponse(
            {"error": "Failed to finalize sheet"}, status_code=500,
        )

    # Filename : Production_{produit slug}_{lot}.pdf
    produit_slug = (
        detail.produit.replace(" ", "_").replace("/", "-")
        if detail.produit else "sheet"
    )[:40]
    lot_part = f"_{detail.lot}" if detail.lot else ""
    fname = f"Production_{produit_slug}{lot_part}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Sheet-Id": detail.id,
            "X-Sheet-Finalized-At": (
                detail.finalized_at.isoformat()
                if detail.finalized_at else ""
            ),
        },
    )


async def _v1_get_production_sheet_pdf(sheet_id: str, request: Request):
    """Re-télécharge le PDF d'une fiche déjà finalisée. ADMIN only.

    Sert pour : ré-imprimer, re-partager, archivage. Le PDF est servi tel
    quel depuis ``pdf_bytes`` (pas de régénération — c'est l'archive de
    référence figée au moment de la finalisation).

    Retour 200 : PDF binaire.
    Retour 404 : fiche introuvable, hors tenant, ou pas de PDF (jamais
    finalisée).
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")

    from common.services.production_sheet_service import (
        get_sheet,
        get_sheet_pdf,
    )

    pdf_bytes = await asyncio.to_thread(
        get_sheet_pdf, user["tenant_id"], sheet_id,
    )
    if not pdf_bytes:
        return JSONResponse(
            {"error": "PDF not found (sheet not finalized?)"}, status_code=404,
        )
    # Headers meta : on relit la fiche pour le filename. Léger surcoût mais
    # le PDF est déjà en RAM, la requête est rare (download manuel).
    detail = await asyncio.to_thread(get_sheet, user["tenant_id"], sheet_id)
    produit_slug = (
        (detail.produit if detail else "").replace(" ", "_").replace("/", "-")
    )[:40] or "sheet"
    lot_part = f"_{detail.lot}" if detail and detail.lot else ""
    fname = f"Production_{produit_slug}{lot_part}.pdf"

    # Audit : qui a téléchargé quelle fiche prod, quand. Admin-only mais
    # traçable comme tout document officiel.
    from common.audit import ACTION_PDF_DOWNLOADED, log_event
    log_event(
        tenant_id=user["tenant_id"],
        user_email=user.get("email") or None,
        action=ACTION_PDF_DOWNLOADED,
        details={
            "type": "production_sheet",
            "sheet_id": sheet_id,
            "lot": (detail.lot if detail else None),
            "filename": fname,
        },
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{fname}"',
            "X-Sheet-Id": sheet_id,
        },
    )


async def _v1_easybeer_brassin_detail(id_brassin: int, request: Request):
    """Détail complet d'un brassin EasyBeer (cache transparent). ADMIN only.

    Proxy vers ``common.easybeer.get_brassin_detail(id)`` qui gère 3 niveaux
    de cache (L1 in-memory 10 min + L2 DB ``eb_cache`` + L3 API EasyBeer).
    L'app iOS appelle cet endpoint pour pré-remplir la recette + étapes +
    planification conditionnement à la création / consultation d'une fiche.

    Retour 200 : payload EasyBeer brut (forme variable selon EB). Les clés
    typiquement attendues : ``recette`` (ingrédients), ``etapes``,
    ``planificationConditionnement``, ``dateDebut``, ``dateConditionnementPrevue``...

    Retour 502 : EasyBeer down ET pas de cache disponible.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")

    from common.easybeer import get_brassin_detail

    try:
        detail = await asyncio.to_thread(get_brassin_detail, id_brassin)
    except Exception as exc:
        _log.exception("Échec récupération brassin EB id=%s", id_brassin)
        return JSONResponse(
            {"error": f"EasyBeer unavailable: {exc}"}, status_code=502,
        )
    if not detail:
        return JSONResponse({"error": "Brassin not found"}, status_code=404)
    return JSONResponse(detail)


async def _v1_admin_cuves(request: Request):
    """Registre des cuves + tables de calibration volume↔hauteur. ADMIN only.

    Sert à l'app iOS pour : (1) proposer le choix de cuve à l'opérateur
    sur la fiche de production, (2) interpoler localement le niveau de
    liquide à partir du volume de remplissage.

    Retour 200 : ``{"cuves": [...], "calibration": {...}}`` — cf.
    ``common.services.cuve_service.get_cuves``.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")

    from common.services.cuve_service import get_cuves

    return JSONResponse(await asyncio.to_thread(get_cuves))
