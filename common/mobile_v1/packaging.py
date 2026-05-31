from __future__ import annotations

import asyncio
import datetime as _dt_local
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from common.mobile_v1 import (
    _resolve_mobile_user,
    _scrub_email,
    _unauthorized,
)
from common.mobile_v1.labels import _DEFAULT_DESTINATAIRE

_log = logging.getLogger("ferment.mobile_v1")


async def _v1_last_packaging(request: Request):
    """Renvoie les emballages habituels d'un destinataire + items configurés.

    Query : ``?destinataire=SOFRIPA`` (défaut: SOFRIPA — seul destinataire mobile).

    Retour 200 :
      ``{"destinataire": "SOFRIPA",
         "items": [{id, label, unit, active}, ...],
         "last_quantities": [{label, qty, unit}, ...]}``.

    L'app iOS utilise ``items`` pour afficher les inputs et ``last_quantities``
    pour le bouton "Appliquer les quantités habituelles".
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    destinataire = (
        request.query_params.get("destinataire") or _DEFAULT_DESTINATAIRE
    ).strip()

    from common.ramasse import load_packaging_items
    from common.ramasse_history import get_last_packaging_for_dest

    items, last = await asyncio.gather(
        asyncio.to_thread(load_packaging_items, destinataire),
        asyncio.to_thread(
            get_last_packaging_for_dest, destinataire, user["tenant_id"],
        ),
    )
    return JSONResponse({
        "destinataire": destinataire,
        "items": items,
        "last_quantities": last,
    })


async def _v1_packaging_request(request: Request):
    """Envoie une demande d'emballages à un destinataire (sans ramasse).

    Body JSON :
      ``{"date_ramasse": "YYYY-MM-DD",
         "items": [{label, qty, unit?}, ...],
         "destinataire": "SOFRIPA"}``  # optionnel, défaut SOFRIPA

    Workflow : feature séparée du formulaire prévisionnel ramasse. L'opérateur
    Symbiose demande des emballages vides (palettes, cagettes…) à recevoir
    lors de la prochaine ramasse (livraison combinée). Génère un email à
    SOFRIPA sans PDF — la demande est suffisamment courte pour rester dans
    le corps de l'email.

    Retour 200 : ``{"email_sent", "recipients", "items_count",
                    "destinataire", "date_ramasse"}``.
    Retour 400 : body malformé / date invalide / items vides.
    Retour 409 : destinataire inconnu ou pas d'emails configurés.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    body = body or {}

    date_ramasse_str = str(body.get("date_ramasse") or "").strip()
    items = body.get("items") or []
    destinataire = str(
        body.get("destinataire") or _DEFAULT_DESTINATAIRE,
    ).strip()

    if not date_ramasse_str:
        return JSONResponse({"error": "Missing 'date_ramasse'"}, status_code=400)
    if not isinstance(items, list):
        return JSONResponse(
            {"error": "'items' must be a list"}, status_code=400,
        )

    try:
        date_ramasse = _dt_local.date.fromisoformat(date_ramasse_str[:10])
    except ValueError:
        return JSONResponse(
            {"error": "Invalid date_ramasse format (expected YYYY-MM-DD)"},
            status_code=400,
        )

    from common.services.loading_service import send_packaging_request

    try:
        result = await asyncio.to_thread(
            send_packaging_request,
            user["tenant_id"],
            user_email=user.get("email") or "",
            destinataire=destinataire,
            date_ramasse=date_ramasse,
            items=items,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception:
        _log.exception("Échec demande emballages (mobile)")
        return JSONResponse(
            {"error": "Failed to send packaging request"}, status_code=500,
        )

    _log.info(
        "packaging_request : dest=%s items=%d email_sent=%s "
        "tenant=%s user=%s",
        destinataire, result["items_count"], result["email_sent"],
        user["tenant_id"], _scrub_email(user.get("email")),
    )
    return JSONResponse(result)


async def _v1_pending_packaging_requests(request: Request):
    """Liste les demandes d'emballages encore à honorer (date >= aujourd'hui).

    Query : ``?destinataire=SOFRIPA`` (optionnel — sinon tous destinataires).

    Sert au bloc "Emballages à ramener" du home iOS : montre à l'opérateur
    les demandes envoyées pour lesquelles SOFRIPA n'a pas encore livré
    (jour de la prochaine ramasse). Auto-nettoyage : passé la date, la ligne
    disparaît automatiquement (on suppose que la livraison a eu lieu).

    Retour 200 :
      ``{"requests": [{id, created_at, user_email, destinataire,
                       date_ramasse, items: [{label, qty, unit}, ...]}, ...]}``.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    destinataire = request.query_params.get("destinataire")
    if destinataire is not None:
        destinataire = destinataire.strip() or None

    from common.services.loading_service import list_pending_packaging_requests

    rows = await asyncio.to_thread(
        list_pending_packaging_requests,
        user["tenant_id"],
        destinataire=destinataire,
    )
    return JSONResponse({"requests": rows})


async def _v1_packaging_requests_history(request: Request):
    """Historique paginé des demandes d'emballages (avec état de livraison).

    Query : ``?limit=20&offset=0&destinataire=SOFRIPA`` (tous optionnels).

    Retour 200 :
      ``{"requests": [{id, created_at, user_email, destinataire,
                       date_ramasse, items: [...],
                       delivered: bool, delivered_at: "..."|null,
                       delivered_by_email: "..."|null}, ...],
         "total": N, "limit": 20, "offset": 0}``.

    Sert au bouton "Historique" depuis l'écran des demandes en attente —
    l'opérateur peut voir l'ensemble des demandes émises avec leur état.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

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

    destinataire = params.get("destinataire")
    if destinataire is not None:
        destinataire = destinataire.strip() or None

    from common.services.loading_service import list_all_packaging_requests

    rows, total = await asyncio.to_thread(
        list_all_packaging_requests,
        user["tenant_id"],
        limit=limit, offset=offset, destinataire=destinataire,
    )
    return JSONResponse({
        "requests": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


async def _v1_mark_packaging_request_delivered(request: Request):
    """Marque une demande d'emballages comme livrée (reçue par l'opérateur).

    Path : ``request_id`` = id audit_log de la demande d'origine.
    Body : ignoré (action atomique sans paramètre).

    Côté serveur : insère une ligne ``audit_log`` action
    ``packaging_request_delivered`` qui supersede la demande d'origine ;
    la liste pending l'exclut automatiquement.

    Retour 200 : ``{"ok": true, "request_id": "..."}``.
    Retour 404 : demande introuvable pour ce tenant.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    request_id = request.path_params.get("request_id") or ""

    from common.services.loading_service import mark_packaging_request_delivered

    ok = await asyncio.to_thread(
        mark_packaging_request_delivered,
        user["tenant_id"],
        request_id=request_id,
        user_email=user.get("email") or "",
    )
    if not ok:
        return JSONResponse(
            {"error": "Packaging request not found"}, status_code=404,
        )
    return JSONResponse({"ok": True, "request_id": request_id})
