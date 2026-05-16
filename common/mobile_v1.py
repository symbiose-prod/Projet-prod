"""
common/mobile_v1.py
===================
Routes ``/api/v1/*`` pour l'app iOS native "Ferment station".

Ce module regroupe TOUS les endpoints destinés au client mobile :

| Méthode | Route                            | Auth | Rôle                                       |
|---------|----------------------------------|------|--------------------------------------------|
| POST    | ``/api/v1/auth/login``           | —    | email+password → token Bearer              |
| POST    | ``/api/v1/auth/logout``          | Tk   | révoque le token courant                   |
| POST    | ``/api/v1/decode-gs1``           | Tk   | décode string GS1-128 + lookup produit     |
| POST    | ``/api/v1/print-palette``        | Tk   | génère SSCC + PDF étiquette palette        |
| POST    | ``/api/v1/labels/{id}/archive``  | Tk   | toggle archive (réversible)                |
| POST    | ``/api/v1/labels/{id}/reprint``  | Tk   | régénère le PDF d'une étiquette historisée |
| GET     | ``/api/v1/today-labels``         | Tk   | étiquettes du jour (toutes, archivées incl.)|
| GET     | ``/api/v1/home-summary``         | Tk   | compteurs jour/mois + 20 dernières         |
| GET     | ``/api/v1/sscc-log``             | Tk*  | journal SSCC (admin uniquement)            |

Tk = Bearer token via `mobile_api_tokens`. Tk* = en plus, rôle = admin.

Toutes les routes :
- Bypassent le middleware d'auth NiceGUI (cookies session) via ``PUBLIC_PATHS``
  configuré dans ``app_nicegui.py`` (préfixe ``/api/v1/``).
- Font leur propre vérification Bearer via ``_resolve_mobile_user``.
- Renvoient un JSON ``{"error": "..."}`` en cas d'échec (status code approprié).
- Sont scopées au ``tenant_id`` du user authentifié (sécurité multi-tenant).

Pour ajouter une route mobile :
  1. Définir la fonction async ``_v1_xxx(request)`` ci-dessous.
  2. La décorer ``@app.post("/api/v1/xxx")`` dans ``register_routes()``.
  3. Documenter dans le tableau ci-dessus + dans CLAUDE.md.

La logique métier vit dans ``common/services/`` — ce fichier n'est qu'un
adapteur "transport HTTP" qui :
- parse les params/body,
- appelle un service,
- formate la réponse JSON.

Pas de SQL inline ici. Si tu en ajoutes un, c'est probablement à pousser
dans un service.
"""
from __future__ import annotations

import asyncio
import datetime as _dt_local
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from common.mobile_auth import (
    create_mobile_token,
    extract_bearer_token,
    revoke_mobile_token,
    verify_mobile_token,
)

_log = logging.getLogger("ferment.mobile_v1")


# ─── Authentification — helper partagé ─────────────────────────────────────

async def _resolve_mobile_user(request: Request) -> dict | None:
    """Résout l'utilisateur courant via ``Authorization: Bearer <token>``.

    Retourne le dict user (``{id, tenant_id, email, role}``) ou ``None``.
    Le caller renvoie 401 si None.
    """
    token = extract_bearer_token(request.headers.get("authorization"))
    if not token:
        return None
    return await asyncio.to_thread(verify_mobile_token, token)


def _unauthorized() -> JSONResponse:
    """Réponse 401 standardisée pour les routes mobile."""
    return JSONResponse({"error": "Invalid or expired token"}, status_code=401)


def _forbidden(reason: str = "Forbidden") -> JSONResponse:
    """Réponse 403 standardisée."""
    return JSONResponse({"error": reason}, status_code=403)


# ─── Auth : login / logout ─────────────────────────────────────────────────

async def _v1_login(request: Request):
    """Login mobile : email + password → token Bearer + infos user.

    Body JSON : ``{"email": "...", "password": "...", "device_name": "..."}``.
    Retour 200 : ``{token, expires_at, tenant_id, user: {email, role}}``.
    Retour 401 : ``{"error": "Invalid credentials"}``.

    Le verrouillage brute-force (table ``login_failures``) s'applique
    comme pour le login web — c'est ``authenticate()`` qui le gère.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    body = body or {}

    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    device_name = body.get("device_name") or ""

    if not email or not password:
        return JSONResponse({"error": "Missing email or password"}, status_code=400)

    from common.auth import authenticate

    user = await asyncio.to_thread(authenticate, email, password)
    if user is None:
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    user_id = str(user["id"])
    tenant_id = str(user["tenant_id"])
    token, expires_at = await asyncio.to_thread(
        create_mobile_token, user_id, tenant_id, device_name
    )

    return JSONResponse({
        "token": token,
        "expires_at": expires_at.isoformat(),
        "tenant_id": tenant_id,
        "user": {
            "email": user["email"],
            "role": user.get("role") or "",
        },
    })


async def _v1_logout(request: Request):
    """Révoque le token Bearer fourni. Idempotent."""
    token = extract_bearer_token(request.headers.get("authorization"))
    if not token:
        return JSONResponse({"error": "Missing token"}, status_code=400)
    await asyncio.to_thread(revoke_mobile_token, token)
    return JSONResponse({"ok": True})


# ─── GS1 : décodage côté serveur après scan caméra ─────────────────────────

async def _v1_decode_gs1(request: Request):
    """Décode une string GS1-128 (scannée nativement côté iPhone) et enrichit
    avec les infos produit + layout palette + URL image.

    Body JSON : ``{"code": "..."}``.
    Retour 200 :
      ``{"ean": "...", "lot": "...", "ddm": "YYYY-MM-DD" | null,
         "product": {designation, marque, fmt, pcb, gout, bottle_type,
                     image_url, palette_layout: {layers, per_layer, total}} | null}``
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    code = ((body or {}).get("code") or "").strip()
    if not code:
        return JSONResponse({"error": "Missing 'code' field"}, status_code=400)
    if len(code) > 200:
        return JSONResponse({"error": "Code too long"}, status_code=400)

    from common.ramasse import get_palette_layout
    from common.services.etiquette_palette_service import (
        get_product_image_url,
        lookup_product_by_ean,
        parse_gs1_to_entry,
    )

    parsed = await asyncio.to_thread(parse_gs1_to_entry, code)
    if parsed is None:
        _log.info("decode-gs1 : parse échoué pour code=%r", code[:60])
        return JSONResponse({"error": "Could not parse GS1 code"}, status_code=400)

    ean = str(parsed["ean"])
    product = await asyncio.to_thread(lookup_product_by_ean, ean)

    # Enrichissement pour l'UI mobile (image + layout palette).
    if product:
        product["image_url"] = get_product_image_url(product.get("gout"))
        layout = get_palette_layout(
            product.get("fmt") or "",
            product.get("designation") or "",
        )
        product["palette_layout"] = {
            "layers": layout.get("layers") or 0,
            "per_layer": layout.get("per_layer") or 0,
            "total": layout.get("total") or 0,
        }

    _log.info(
        "decode-gs1 : ean=%s lot=%s product=%s user=%s",
        ean,
        parsed.get("lot") or "—",
        (product or {}).get("designation") or "(non trouvé EB)",
        user.get("email"),
    )

    return JSONResponse({
        "ean": ean,
        "lot": parsed.get("lot") or "",
        "ddm": parsed.get("ddm") or None,
        "product": product,
    })


# ─── Génération PDF étiquette palette ──────────────────────────────────────

async def _v1_print_palette(request: Request):
    """Génère un PDF d'étiquette palette pour le scan + saisie courante.

    Body JSON :
      ``{"ean": "...", "lot": "...", "ddm": "YYYY-MM-DD",
         "case_count": 12, "full_pallet": true, "n_copies": 1}``.

    Retour 200 : binaire PDF (Content-Type: application/pdf).
    Retour 404 : produit introuvable (EAN absent matrice EasyBeer).

    Délègue tout à ``generate_and_save_palette_label`` (service partagé
    avec la page web) — SSCC + PDF + audit + purge dans la même transaction
    logique.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    body = body or {}

    ean = str(body.get("ean") or "").strip()
    lot = str(body.get("lot") or "").strip()
    ddm_str = str(body.get("ddm") or "").strip()
    case_count = int(body.get("case_count") or 0)
    full_pallet = bool(body.get("full_pallet", False))
    n_copies = int(body.get("n_copies") or 1)

    if not ean or not lot or not ddm_str:
        return JSONResponse({"error": "Missing ean/lot/ddm"}, status_code=400)
    if case_count <= 0:
        return JSONResponse({"error": "case_count must be > 0"}, status_code=400)
    if n_copies < 1 or n_copies > 10:
        return JSONResponse({"error": "n_copies must be between 1 and 10"}, status_code=400)

    try:
        ddm = _dt_local.date.fromisoformat(ddm_str[:10])
    except ValueError:
        return JSONResponse({"error": "Invalid ddm format (expected YYYY-MM-DD)"}, status_code=400)

    from common.services.etiquette_palette_service import (
        ProductNotFoundError,
        generate_and_save_palette_label,
    )

    try:
        pdf_bytes, sscc, label_id = await asyncio.to_thread(
            generate_and_save_palette_label,
            user["tenant_id"],
            user_email=user.get("email") or "",
            ean=ean,
            lot=lot,
            ddm=ddm,
            case_count=case_count,
            full_pallet=full_pallet,
            n_copies=n_copies,
        )
    except ProductNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception:
        _log.exception("Echec build PDF étiquette palette (mobile)")
        return JSONResponse({"error": "PDF generation failed"}, status_code=500)

    _log.info(
        "print-palette : ean=%s qty=%d copies=%d sscc=%s label_id=%s user=%s",
        ean, case_count, n_copies, sscc, label_id, user.get("email"),
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="palette_{ean}_{lot}.pdf"'},
    )


# ─── Étiquettes (archive / reprint) ────────────────────────────────────────

async def _v1_archive_label(label_id: int, request: Request):
    """Toggle l'état archivé d'une étiquette.

    Body JSON optionnel : ``{"archived": true|false}`` pour forcer la valeur.
    Sans body → toggle simple. L'étiquette doit appartenir au tenant du token
    (sinon 404 silencieux, pas d'info leak).
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    desired: bool | None = None
    try:
        body = await request.json()
        if isinstance(body, dict) and "archived" in body:
            desired = bool(body["archived"])
    except Exception:
        pass

    from common.services.etiquette_palette_service import set_label_archived

    result = await asyncio.to_thread(
        set_label_archived,
        user["tenant_id"],
        label_id,
        archived=desired,
    )
    if result is False:
        return JSONResponse({"error": "Label not found"}, status_code=404)
    archived_at = result if hasattr(result, "isoformat") else None
    _log.info(
        "label archive : id=%s archived=%s tenant=%s user=%s",
        label_id, archived_at is not None, user["tenant_id"], user.get("email"),
    )
    return JSONResponse({
        "id": label_id,
        "archived_at": archived_at.isoformat() if archived_at else None,
    })


async def _v1_reprint_label(label_id: int, request: Request):
    """Régénère le PDF d'une étiquette historisée (réimpression à l'identique).

    N'INSERT pas de nouvelle ligne dans l'historique — c'est la même palette
    physique avec le même SSCC. Retourne le PDF.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    from common.etiquette_palette_pdf import EtiquetteContext, build_etiquette_palette_pdf
    from common.services.etiquette_palette_service import get_history_entry

    entry = await asyncio.to_thread(get_history_entry, user["tenant_id"], label_id)
    if entry is None:
        return JSONResponse({"error": "Label not found"}, status_code=404)

    ctx = EtiquetteContext(
        product_label=entry.designation or "",
        fmt=entry.fmt or "",
        ean13=entry.ean or "",
        lot=entry.lot or "",
        ddm=entry.ddm,
        case_count=entry.case_count,
        full_pallet=entry.full_pallet,
        tenant_name="",
        n_copies=entry.n_copies,
        marque=entry.marque or "",
        code_interne=entry.code_interne or "",
        gtin_uvc=entry.gtin_uvc or "",
        pcb=entry.pcb,
        bio=entry.bio,
        sscc=entry.sscc or "",
    )

    try:
        pdf_bytes = await asyncio.to_thread(build_etiquette_palette_pdf, ctx)
    except Exception:
        _log.exception("Echec réimpression PDF label_id=%s", label_id)
        return JSONResponse({"error": "PDF generation failed"}, status_code=500)

    _log.info(
        "label reprint : id=%s tenant=%s user=%s",
        label_id, user["tenant_id"], user.get("email"),
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="palette_{entry.ean}_{entry.lot}_reprint.pdf"',
        },
    )


# ─── Listings : aujourd'hui + résumé accueil ───────────────────────────────

async def _v1_today_labels(request: Request):
    """Liste des étiquettes générées aujourd'hui (toutes, archivées incluses)."""
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    from common.services.etiquette_palette_service import list_today_labels

    labels = await asyncio.to_thread(list_today_labels, user["tenant_id"])
    return JSONResponse({"labels": labels})


async def _v1_home_summary(request: Request):
    """Résumé accueil : compteurs du jour/mois + 20 derniers scans.

    Combine ``count_today_and_month()`` + ``list_recent_labels()``.
    Une seule réponse pour limiter les round-trips au démarrage de la home.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    from common.services.etiquette_palette_service import (
        count_today_and_month,
        list_recent_labels,
    )

    counts, entries = await asyncio.gather(
        asyncio.to_thread(count_today_and_month, user["tenant_id"]),
        asyncio.to_thread(list_recent_labels, user["tenant_id"], 20),
    )
    recent = [
        {
            "id": e.id,
            "designation": e.designation,
            "marque": e.marque,
            "fmt": e.fmt,
            "gout": e.gout,
            "case_count": e.case_count,
            "generated_at": e.generated_at.isoformat() if e.generated_at else None,
        }
        for e in entries
    ]
    return JSONResponse({
        "today_count": counts["today_count"],
        "month_count": counts["month_count"],
        "recent": recent,
    })


# ─── Journal SSCC (admin) ──────────────────────────────────────────────────

async def _v1_sscc_log(request: Request):
    """Journal SSCC pour le tenant courant — admin uniquement.

    Query params : ``limit`` (défaut 50, max 500), ``date_from``,
    ``date_to`` (YYYY-MM-DD), ``lot`` (motif ILIKE).
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")

    params = request.query_params
    try:
        limit = int(params.get("limit") or "50")
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 500))

    def _parse_date(s: str | None) -> _dt_local.date | None:
        if not s:
            return None
        try:
            return _dt_local.date.fromisoformat(s[:10])
        except ValueError:
            return None

    date_from = _parse_date(params.get("date_from"))
    date_to = _parse_date(params.get("date_to"))
    lot_filter = (params.get("lot") or "").strip()

    from common.services.sscc_service import list_sscc_log

    entries = await asyncio.to_thread(
        list_sscc_log,
        user["tenant_id"],
        date_from=date_from,
        date_to=date_to,
        lot_filter=lot_filter,
        limit=limit,
    )

    payload = [
        {
            "id": e.id,
            "sscc": e.sscc,
            "user_email": e.user_email,
            "gtin_palette": e.gtin_palette,
            "lot": e.lot,
            "ddm": e.ddm.isoformat() if e.ddm else None,
            "case_count": e.case_count,
            "generated_at": e.generated_at.isoformat() if e.generated_at else None,
            "voided_at": e.voided_at.isoformat() if e.voided_at else None,
            "voided_reason": e.voided_reason,
            "voided_by": e.voided_by,
            "ramasse_id": e.ramasse_id,
            "ramasse_date": e.ramasse_date.isoformat() if e.ramasse_date else None,
            "ramasse_destinataire": e.ramasse_destinataire,
            "loaded_at": e.loaded_at.isoformat() if e.loaded_at else None,
            "label_id": e.label_id,
            "label_archived_at": e.label_archived_at.isoformat() if e.label_archived_at else None,
            "designation": e.designation,
            "marque": e.marque,
            "gout": e.gout,
            "ramasse_numero": e.ramasse_numero,
        }
        for e in entries
    ]
    return JSONResponse({"entries": payload})


# ─── Enregistrement des routes (appelé depuis app_nicegui.py) ──────────────

def register_routes(app) -> None:
    """Enregistre les endpoints ``/api/v1/*`` sur l'app NiceGUI/FastAPI.

    Appeler une fois au démarrage depuis ``app_nicegui.py``, après la
    configuration du middleware d'auth.
    """
    app.post("/api/v1/auth/login")(_v1_login)
    app.post("/api/v1/auth/logout")(_v1_logout)
    app.post("/api/v1/decode-gs1")(_v1_decode_gs1)
    app.post("/api/v1/print-palette")(_v1_print_palette)
    app.post("/api/v1/labels/{label_id}/archive")(_v1_archive_label)
    app.post("/api/v1/labels/{label_id}/reprint")(_v1_reprint_label)
    app.get("/api/v1/today-labels")(_v1_today_labels)
    app.get("/api/v1/home-summary")(_v1_home_summary)
    app.get("/api/v1/sscc-log")(_v1_sscc_log)
