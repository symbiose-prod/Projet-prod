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
| GET     | ``/api/v1/cold-room-palettes``   | Tk   | palettes étiquetées non encore chargées    |
| GET     | ``/api/v1/last-packaging``       | Tk   | emballages habituels du destinataire + items configurés |
| GET     | ``/api/v1/active-ramasses``      | Tk   | ramasses ``previsionnel`` ouvertes (J2 reprise) |
| POST    | ``/api/v1/loadings/previsionnel`` | Tk  | crée + envoie BL prévisionnel (J1 soir)    |
| GET     | ``/api/v1/loadings/{id}``        | Tk   | détail d'un chargement (palettes liées + totaux) |
| POST    | ``/api/v1/loadings/{id}/scan``   | Tk   | scan SSCC + lien à la ramasse (chargement J2) |
| POST    | ``/api/v1/loadings/{id}/finalize`` | Tk | finalise prévisionnel → définitif + PDF BL + email |
| DELETE  | ``/api/v1/loadings/{id}/palettes/{sscc}`` | Tk | délie une palette d'une ramasse (soft) |
| GET     | ``/api/v1/ramasses``             | Tk   | historique paginé des ramasses (toutes statuts, hors corbeille) |
| GET     | ``/api/v1/ramasses/{id}/pdf``    | Tk   | PDF BL stocké (renvoie le ``pdf_bytes`` de la dernière version) |
| POST    | ``/api/v1/ramasses/{id}/mark-driver-passed`` | Tk | marque "chauffeur passé" → verrouille l'édition |
| POST    | ``/api/v1/admin/production-sheets`` | Tk* | crée une fiche de production (status draft) |
| GET     | ``/api/v1/admin/production-sheets`` | Tk* | liste paginée des fiches du tenant |
| GET     | ``/api/v1/admin/brassins-en-cours`` | Tk* | brassins actifs EasyBeer (pour pré-remplir une fiche) |
| GET     | ``/api/v1/admin/conditionnement-by-lot?lot=...`` | Tk* | agrégation SSCC par (format, marque) pour un lot |
| GET     | ``/api/v1/admin/production-sheets/{id}`` | Tk* | détail complet d'une fiche (avec data JSONB) |
| PATCH   | ``/api/v1/admin/production-sheets/{id}`` | Tk* | mise à jour partielle (auto-save iOS) |
| GET     | ``/api/v1/admin/production-overview`` | Tk* | brassins EB en cours + fiches draft associées (1 appel) |
| GET     | ``/api/v1/admin/easybeer/brassin/{id}`` | Tk* | détail brassin EB (cache 3 niveaux) |
| POST    | ``/api/v1/admin/production-sheets/{id}/finalize`` | Tk* | finalise (génère PDF + status completed) |
| GET     | ``/api/v1/admin/production-sheets/{id}/pdf`` | Tk* | re-télécharge le PDF stocké |

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

async def _v1_preview_palette(request: Request):
    """Génère un PDF d'aperçu pour validation visuelle AVANT impression réelle.

    Body : ``{"ean", "lot", "ddm", "case_count", "full_pallet", "n_copies"}``
    (mêmes champs que /print-palette).

    Retour 200 : PDF binaire. Aucun SSCC consommé, aucun audit créé.
    Le SSCC affiché sur le PDF est vide → la section SSCC est masquée,
    l'opérateur voit visuellement qu'il s'agit d'un aperçu.

    Use case : opérateur en formation, ou validation visuelle d'un layout
    avant impression réelle. Ne sale pas la séquence ``sscc_serial_seq``.
    """
    import datetime as _dt_local

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

    try:
        ddm = _dt_local.date.fromisoformat(ddm_str[:10])
    except ValueError:
        return JSONResponse({"error": "Invalid ddm format"}, status_code=400)

    from common.services.etiquette_palette_service import (
        ProductNotFoundError,
        preview_palette_label,
    )

    try:
        pdf_bytes = await asyncio.to_thread(
            preview_palette_label,
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
        _log.exception("Echec preview PDF étiquette palette")
        return JSONResponse({"error": "PDF generation failed"}, status_code=500)

    _log.info(
        "preview-palette : ean=%s qty=%d user=%s (no SSCC, no history)",
        ean, case_count, user.get("email"),
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="palette_{ean}_{lot}_PREVIEW.pdf"',
        },
    )


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


# ─── Chargement camion (ramasse) ───────────────────────────────────────────

def _palette_to_dict(p) -> dict:
    """Sérialise un ``PaletteInfo`` en dict JSON-compatible.

    Centralisé pour garantir le même format sur tous les endpoints loading
    (scan-sscc, cold-room-palettes, loadings/{id}). L'app iOS décode ce dict
    en ``PaletteInfo`` Swift Codable.
    """
    return {
        "sscc": p.sscc,
        "gtin_palette": p.gtin_palette,
        "lot": p.lot,
        "ddm": p.ddm.isoformat() if p.ddm else None,
        "case_count": p.case_count,
        "designation": p.designation,
        "fmt": p.fmt,
        "marque": p.marque,
        "gout": p.gout,
        "pcb": p.pcb,
        "gtin_uvc": p.gtin_uvc,
        "generated_at": p.generated_at.isoformat() if p.generated_at else None,
    }


_DEFAULT_DESTINATAIRE = "SOFRIPA"
"""Destinataire unique côté mobile. La page web supporte plusieurs destinataires,
mais en pratique côté terrain c'est toujours SOFRIPA (logisticien Ferment).
Si on veut en supporter d'autres, ajouter un sélecteur côté iOS + query param
sur les endpoints concernés."""


def _lookup_result_to_dict(result) -> dict:
    """Sérialise un ``LookupResult`` en dict JSON-compatible."""
    return {
        "status": result.status,
        "palette": _palette_to_dict(result.palette) if result.palette else None,
        "existing_ramasse_id": result.existing_ramasse_id,
        "existing_scanned_at": (
            result.existing_scanned_at.isoformat()
            if result.existing_scanned_at else None
        ),
        "error_message": result.error_message,
    }


async def _v1_cold_room_palettes(request: Request):
    """Liste les palettes en chambre froide non encore chargées.

    Retour 200 : ``{"palettes": [{sscc, designation, ...}, ...]}``.
    Tri FIFO par ``generated_at`` croissant (les plus anciennes en haut).
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    from common.services.loading_service import list_palettes_in_cold_room

    palettes = await asyncio.to_thread(
        list_palettes_in_cold_room, user["tenant_id"],
    )
    return JSONResponse({
        "palettes": [_palette_to_dict(p) for p in palettes],
    })


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


async def _v1_active_ramasses(request: Request):
    """Liste les ramasses ``previsionnel`` (ou ``definitif`` non livré) ouvertes.

    Query : ``?destinataire=SOFRIPA`` (défaut: SOFRIPA).

    Sert au J2 : l'opérateur ouvre l'app, l'iPad lui affiche les ramasses
    prêtes à être chargées. En pratique il n'y en a qu'une (verrou métier).

    Retour 200 : ``{"ramasses": [{id, date_ramasse, status, total_palettes,
                                  ...}, ...]}``.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    destinataire = (
        request.query_params.get("destinataire") or _DEFAULT_DESTINATAIRE
    ).strip()

    from common.ramasse_history import get_active_ramasse_for_dest

    active = await asyncio.to_thread(
        get_active_ramasse_for_dest, destinataire, user["tenant_id"],
    )
    ramasses: list[dict] = []
    if active:
        ramasses.append({
            "id": str(active["id"]),
            "date_ramasse": (
                active["date_ramasse"].isoformat()
                if active.get("date_ramasse") else None
            ),
            "destinataire": active.get("destinataire") or "",
            "status": active.get("status") or "",
            "total_palettes": int(active.get("total_palettes") or 0),
            "total_cartons": int(active.get("total_cartons") or 0),
            "total_poids_kg": int(active.get("total_poids_kg") or 0),
            "version": int(active.get("version") or 1),
            "created_by_email": active.get("created_by_email") or "",
            "created_at": (
                active["created_at"].isoformat()
                if active.get("created_at") else None
            ),
        })
    return JSONResponse({"ramasses": ramasses})


async def _v1_create_previsionnel(request: Request):
    """Crée et envoie un BL prévisionnel (J1 soir).

    Body JSON :
      ``{"date_ramasse": "YYYY-MM-DD",
         "sscc_list": ["...", ...],            # palettes à inclure
         "packaging": [{label, qty, unit?}, ...],  # emballages à ramener
         "destinataire": "SOFRIPA"}``  # optionnel, défaut SOFRIPA

    Délègue toute l'orchestration (save_ramasse + link + PDF + email) à
    ``loading_service.send_previsionnel``. L'endpoint n'est qu'un adaptateur
    HTTP qui parse le body et formate la réponse.

    Retour 200 : ``{"id", "total_palettes", "total_cartons", "total_poids_kg",
                    "inserted", "conflicts", "email_sent", "recipients"}``.
    Retour 409 : verrou métier (ramasse active existe déjà) — l'opérateur
    doit finaliser/supprimer l'ancienne d'abord.
    Retour 400 : destinataire inconnu ou pas d'emails configurés.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    body = body or {}

    sscc_list_raw = body.get("sscc_list") or []
    date_ramasse_str = str(body.get("date_ramasse") or "").strip()
    packaging = body.get("packaging") or []
    destinataire = str(
        body.get("destinataire") or _DEFAULT_DESTINATAIRE,
    ).strip()

    if not isinstance(sscc_list_raw, list):
        return JSONResponse(
            {"error": "'sscc_list' must be a list"}, status_code=400,
        )
    if not date_ramasse_str:
        return JSONResponse({"error": "Missing 'date_ramasse'"}, status_code=400)
    if not isinstance(packaging, list):
        return JSONResponse(
            {"error": "'packaging' must be a list"}, status_code=400,
        )

    try:
        date_ramasse = _dt_local.date.fromisoformat(date_ramasse_str[:10])
    except ValueError:
        return JSONResponse(
            {"error": "Invalid date_ramasse format (expected YYYY-MM-DD)"},
            status_code=400,
        )

    sscc_list = [str(s).strip() for s in sscc_list_raw if str(s).strip()]

    from common.services.loading_service import send_previsionnel

    try:
        result = await asyncio.to_thread(
            send_previsionnel,
            user["tenant_id"],
            user_id=user["id"],
            user_email=user.get("email") or "",
            destinataire=destinataire,
            date_ramasse=date_ramasse,
            sscc_list=sscc_list,
            packaging=packaging,
        )
    except ValueError as exc:
        # Verrou métier OU destinataire inconnu — code 409 dans les 2 cas
        # pour signaler "conflit métier" (vs 400 = body malformé)
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception:
        _log.exception("Échec création prévisionnel (mobile)")
        return JSONResponse(
            {"error": "Failed to create previsionnel"}, status_code=500,
        )

    _log.info(
        "previsionnel : ramasse=%s dest=%s palettes=%d email_sent=%s "
        "tenant=%s user=%s",
        result["id"], destinataire, result["total_palettes"],
        result["email_sent"], user["tenant_id"], user.get("email"),
    )
    return JSONResponse(result)


async def _v1_scan_palette_to_loading(ramasse_id: str, request: Request):
    """Scan SSCC + lien immédiat à une ramasse en cours (J2 chargement).

    Body JSON : ``{"sscc": "..."}``.

    Un appel = un scan douchette. L'iPad scanne une palette, on lookup ET
    on lie en une seule transaction pour avoir le retour temps-réel (palette
    qui "passe de la CF au camion" côté UI).

    Retour 200 :
      Si ``status="ok"`` (palette valide + ajoutée) :
        ``{"status": "ok", "palette": {...}, "linked": true,
           "already_in_this_loading": false}``
      Si déjà liée à cette ramasse (re-scan du même SSCC) :
        ``{"status": "ok", "palette": {...}, "linked": false,
           "already_in_this_loading": true}``
      Si liée à une AUTRE ramasse :
        ``{"status": "already_loaded", "existing_ramasse_id": "...",
           "error_message": "..."}``
      Si inconnue / annulée / inconsistante :
        ``{"status": "unknown"|"inconsistent", "error_message": "..."}``
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    sscc_raw = ((body or {}).get("sscc") or "").strip()
    if not sscc_raw:
        return JSONResponse({"error": "Missing 'sscc' field"}, status_code=400)
    if len(sscc_raw) > 200:
        return JSONResponse({"error": "SSCC too long"}, status_code=400)

    from common.services.loading_service import (
        link_palettes_to_ramasse,
        lookup_sscc,
    )

    result = await asyncio.to_thread(lookup_sscc, sscc_raw, user["tenant_id"])

    # Cas "déjà liée à cette ramasse" = re-scan du même SSCC pendant le
    # chargement. lookup_sscc renvoie 'already_loaded' avec
    # existing_ramasse_id. On distingue : si c'est CETTE ramasse, c'est OK
    # (idempotent) ; sinon c'est une vraie collision avec une autre ramasse.
    if result.status == "already_loaded" and result.existing_ramasse_id == ramasse_id:
        # Récupère la PaletteInfo pour la retourner (lookup_sscc ne la
        # remplit pas en cas already_loaded). On refait un lookup direct
        # via list_linked_palettes — petit coût, mais retour propre.
        from common.services.loading_service import list_linked_palettes
        all_linked = await asyncio.to_thread(
            list_linked_palettes, ramasse_id, user["tenant_id"],
        )
        # On cherche le SSCC dans les palettes liées (en supportant les
        # variantes avec/sans préfixe AI). Comparaison stricte sur 18 digits.
        from common.services.loading_service import _normalize_sscc
        sscc_norm = _normalize_sscc(sscc_raw)
        match = next((p for p in all_linked if p.sscc == sscc_norm), None)
        return JSONResponse({
            "status": "ok",
            "palette": _palette_to_dict(match) if match else None,
            "linked": False,
            "already_in_this_loading": True,
        })

    if result.status != "ok" or result.palette is None:
        # SSCC inconnu, inconsistent, ou déjà chargé ailleurs : on renvoie
        # l'info brute, pas de link tenté.
        return JSONResponse({
            **_lookup_result_to_dict(result),
            "linked": False,
            "already_in_this_loading": False,
        })

    # palette OK et libre → on lie
    inserted, conflicts = await asyncio.to_thread(
        link_palettes_to_ramasse,
        user["tenant_id"],
        sscc_list=[result.palette.sscc],
        ramasse_id=ramasse_id,
        user_email=user.get("email") or "",
    )
    if inserted == 0:
        # Race condition : palette liée entre lookup et insert (très rare)
        return JSONResponse({
            "status": "already_loaded",
            "palette": _palette_to_dict(result.palette),
            "linked": False,
            "already_in_this_loading": False,
            "error_message": "Palette liée entre temps à une autre ramasse",
        })
    _log.info(
        "scan-to-loading : sscc=%s ramasse=%s tenant=%s user=%s",
        result.palette.sscc, ramasse_id, user["tenant_id"], user.get("email"),
    )
    return JSONResponse({
        "status": "ok",
        "palette": _palette_to_dict(result.palette),
        "linked": True,
        "already_in_this_loading": False,
    })


async def _v1_finalize_loading(ramasse_id: str, request: Request):
    """Finalise une ramasse ``previsionnel`` → ``definitif`` + envoie BL.

    Pas de body requis. Délègue à ``loading_service.finalize_loading``.

    Retour 200 : PDF binaire (Content-Type: application/pdf) du BL définitif
    pour download immédiat par le chauffeur. Headers complémentaires :
      - ``X-Ramasse-Id``: UUID de la ramasse
      - ``X-Total-Palettes``: nombre de palettes
      - ``X-Total-Cartons``: nombre de cartons
      - ``X-Email-Sent``: ``true`` / ``false`` selon que l'envoi mail a réussi

    Retour 404 : ramasse introuvable ou hors tenant.
    Retour 409 : ramasse déjà ``definitif`` ou verrouillée (chauffeur passé).
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    from common.services.loading_service import finalize_loading

    try:
        info, pdf_bytes = await asyncio.to_thread(
            finalize_loading,
            user["tenant_id"],
            ramasse_id=ramasse_id,
            user_email=user.get("email") or "",
        )
    except ValueError as exc:
        msg = str(exc)
        # 404 si introuvable, sinon 409 (conflit métier)
        status_code = 404 if "introuvable" in msg.lower() else 409
        return JSONResponse({"error": msg}, status_code=status_code)
    except Exception:
        _log.exception("Échec finalize loading ramasse=%s", ramasse_id)
        return JSONResponse(
            {"error": "Failed to finalize loading"}, status_code=500,
        )

    _log.info(
        "finalize-loading : ramasse=%s palettes=%d cartons=%d email_sent=%s "
        "tenant=%s user=%s",
        info["id"], info["total_palettes"], info["total_cartons"],
        info["email_sent"], user["tenant_id"], user.get("email"),
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="BL_Definitif_{info["id"][:8]}.pdf"'
            ),
            "X-Ramasse-Id": str(info["id"]),
            "X-Total-Palettes": str(info["total_palettes"]),
            "X-Total-Cartons": str(info["total_cartons"]),
            "X-Total-Poids-Kg": str(info["total_poids_kg"]),
            "X-Email-Sent": "true" if info["email_sent"] else "false",
            "X-Ramasse-Version": str(info["version"]),
        },
    )


async def _v1_get_loading(ramasse_id: str, request: Request):
    """Détail d'un chargement : palettes liées + totaux + meta ramasse.

    Retour 200 :
      ``{"id": "...", "date_ramasse": "...", "destinataire": "...",
         "status": "...", "palettes": [...], "total_palettes": N,
         "total_cartons": M, "total_poids_kg": P}``.
    Retour 404 : ramasse inconnue ou hors tenant.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    from common.ramasse_history import get_ramasse
    from common.services.loading_service import list_linked_palettes

    ramasse = await asyncio.to_thread(get_ramasse, ramasse_id, user["tenant_id"])
    if ramasse is None:
        return JSONResponse({"error": "Loading not found"}, status_code=404)

    palettes = await asyncio.to_thread(
        list_linked_palettes, ramasse_id, user["tenant_id"],
    )
    return JSONResponse({
        "id": str(ramasse["id"]),
        "date_ramasse": (
            ramasse["date_ramasse"].isoformat()
            if ramasse.get("date_ramasse") else None
        ),
        "destinataire": ramasse.get("destinataire") or "",
        "status": ramasse.get("status") or "",
        "total_palettes": int(ramasse.get("total_palettes") or 0),
        "total_cartons": int(ramasse.get("total_cartons") or 0),
        "total_poids_kg": int(ramasse.get("total_poids_kg") or 0),
        "palettes": [_palette_to_dict(p) for p in palettes],
    })


async def _v1_list_ramasses(request: Request):
    """Historique paginé des ramasses (toutes statuts, hors corbeille).

    Query : ``?limit=20&offset=0`` (par défaut 20 / 0). Maximum ``limit=100``.

    Retour 200 :
      ``{"ramasses": [{id, date_ramasse, destinataire, status,
                       total_palettes, total_cartons, total_poids_kg,
                       version, driver_passed, driver_passed_at,
                       created_at, has_pdf}, ...],
         "total": N, "limit": 20, "offset": 0}``

    ``has_pdf`` indique si la ramasse a un BL stocké (téléchargeable via
    ``/api/v1/ramasses/{id}/pdf``). Les ramasses récentes auront ``has_pdf=true``,
    les très anciennes legacy peuvent ne pas en avoir.
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

    from common.ramasse_history import count_ramasses, list_ramasses

    rows, total = await asyncio.gather(
        asyncio.to_thread(
            list_ramasses, user["tenant_id"], limit=limit, offset=offset,
        ),
        asyncio.to_thread(count_ramasses, user["tenant_id"]),
    )

    payload = [
        {
            "id": str(r["id"]),
            "date_ramasse": (
                r["date_ramasse"].isoformat()
                if r.get("date_ramasse") else None
            ),
            "destinataire": r.get("destinataire") or "",
            "status": r.get("status") or "",
            "total_palettes": int(r.get("total_palettes") or 0),
            "total_cartons": int(r.get("total_cartons") or 0),
            "total_poids_kg": int(r.get("total_poids_kg") or 0),
            "version": int(r.get("version") or 1),
            "driver_passed": bool(r.get("driver_passed")),
            "driver_passed_at": (
                r["driver_passed_at"].isoformat()
                if r.get("driver_passed_at") else None
            ),
            "created_at": (
                r["created_at"].isoformat()
                if r.get("created_at") else None
            ),
            # list_ramasses omet volontairement pdf_bytes pour la perf,
            # donc on ne peut pas dire ici si le PDF est dispo. On marque
            # true par défaut — le client gère le 404 si pas dispo.
            "has_pdf": True,
        }
        for r in rows
    ]
    return JSONResponse({
        "ramasses": payload,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


async def _v1_ramasse_pdf(ramasse_id: str, request: Request):
    """Renvoie le PDF BL stocké d'une ramasse (dernière version envoyée).

    Le PDF est stocké en colonne ``ramasse_history.pdf_bytes`` au moment de
    la création (prévisionnel) ou de la finalisation (définitif). Pour les
    ramasses qui ont eu une version définitive, c'est ce dernier qui est
    retourné.

    Retour 200 : binaire PDF (Content-Type: application/pdf).
    Retour 404 : ramasse introuvable, hors tenant, ou aucun PDF stocké.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    from common.ramasse_history import get_ramasse

    ramasse = await asyncio.to_thread(get_ramasse, ramasse_id, user["tenant_id"])
    if ramasse is None:
        return JSONResponse({"error": "Ramasse not found"}, status_code=404)
    pdf_bytes = ramasse.get("pdf_bytes")
    if not pdf_bytes:
        return JSONResponse({"error": "No PDF stored for this ramasse"}, status_code=404)

    # Suffixe filename selon statut pour éviter de mélanger les BL côté chauffeur
    status = ramasse.get("status") or "ramasse"
    suffix = "Definitif" if status == "definitif" else "Provisoire"
    date_ramasse = ramasse.get("date_ramasse")
    date_str = date_ramasse.strftime("%Y%m%d") if date_ramasse else ramasse_id[:8]
    fname = f"BL_{suffix}_{date_str}.pdf"

    _log.info(
        "ramasse-pdf : id=%s status=%s tenant=%s user=%s",
        ramasse_id, status, user["tenant_id"], user.get("email"),
    )
    return Response(
        content=bytes(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{fname}"',
            "X-Ramasse-Id": str(ramasse["id"]),
            "X-Ramasse-Status": status,
            "X-Ramasse-Version": str(int(ramasse.get("version") or 1)),
        },
    )


async def _v1_mark_driver_passed(ramasse_id: str, request: Request):
    """Marque une ramasse comme livrée (chauffeur passé) → verrouille l'édition.

    Idempotent : si déjà marqué, retourne 200 ``{"ok": true, "changed": false}``.

    Retour 200 : ``{"ok": true, "changed": bool}`` (changed=false si déjà livré).
    Retour 404 : ramasse introuvable ou hors tenant.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    from common.ramasse_history import get_ramasse, mark_driver_passed

    # On vérifie d'abord l'existence pour distinguer "404 not found"
    # de "déjà marqué" (mark_driver_passed renvoie False dans les 2 cas).
    ramasse = await asyncio.to_thread(get_ramasse, ramasse_id, user["tenant_id"])
    if ramasse is None:
        return JSONResponse({"error": "Ramasse not found"}, status_code=404)

    if ramasse.get("driver_passed"):
        return JSONResponse({"ok": True, "changed": False})

    changed = await asyncio.to_thread(
        mark_driver_passed,
        ramasse_id,
        tenant_id=user["tenant_id"],
        user_id=user["id"],
    )
    _log.info(
        "mark-driver-passed : id=%s changed=%s tenant=%s user=%s",
        ramasse_id, changed, user["tenant_id"], user.get("email"),
    )
    return JSONResponse({"ok": True, "changed": changed})


async def _v1_unlink_palette(ramasse_id: str, sscc: str, request: Request):
    """Délie une palette d'une ramasse (soft-unlink réversible).

    Body JSON optionnel : ``{"reason": "..."}`` — raison saisie par
    l'opérateur (palette cassée, erreur de scan, etc.). Défaut: générique.

    Retour 200 : ``{"ok": true}``.
    Retour 404 : palette pas liée à cette ramasse (ou déjà unlinkée).
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    reason = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            reason = str(body.get("reason") or "").strip()
    except Exception:
        pass

    from common.services.loading_service import unlink_palette

    ok = await asyncio.to_thread(
        unlink_palette,
        user["tenant_id"],
        sscc=sscc,
        ramasse_id=ramasse_id,
        reason=reason,
        user_email=user.get("email") or "",
    )
    if not ok:
        return JSONResponse(
            {"error": "Palette not linked to this loading"}, status_code=404,
        )
    _log.info(
        "unlink-palette : sscc=%s ramasse=%s tenant=%s user=%s",
        sscc, ramasse_id, user["tenant_id"], user.get("email"),
    )
    return JSONResponse({"ok": True})


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

    from common.services.production_sheet_service import (
        compute_real_conditionnement_by_lot,
    )

    result = await asyncio.to_thread(
        compute_real_conditionnement_by_lot, user["tenant_id"], lot,
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
    """Sérialise ProductionSheetDetail en dict JSON pour le mobile."""
    return {
        "id": s.id,
        "brassin_id": s.brassin_id,
        "produit": s.produit,
        "cuve": s.cuve,
        "ddm": s.ddm.isoformat() if s.ddm else None,
        "lot": s.lot,
        "status": s.status,
        "data": s.data,
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

    Retour 200 : ``{"ok": true}`` + fiche complète mise à jour (mêmes
    champs que GET) pour permettre au client de re-synchroniser sans
    requête supplémentaire.
    Retour 400 : body invalide (ex: ddm mal formée).
    Retour 404 : fiche introuvable, hors tenant, ou déjà finalisée.
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

    if not kwargs:
        # Rien à patcher — on renvoie la fiche actuelle (idempotent friendly)
        detail = await asyncio.to_thread(get_sheet, user["tenant_id"], sheet_id)
        if detail is None:
            return JSONResponse({"error": "Sheet not found"}, status_code=404)
        return JSONResponse({"ok": True, "sheet": _serialize_sheet_detail(detail)})

    changed = await asyncio.to_thread(
        patch_sheet, user["tenant_id"], sheet_id, **kwargs,
    )
    if not changed:
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


# ─── Enregistrement des routes (appelé depuis app_nicegui.py) ──────────────

def register_routes(app) -> None:
    """Enregistre les endpoints ``/api/v1/*`` sur l'app NiceGUI/FastAPI.

    Appeler une fois au démarrage depuis ``app_nicegui.py``, après la
    configuration du middleware d'auth.
    """
    app.post("/api/v1/auth/login")(_v1_login)
    app.post("/api/v1/auth/logout")(_v1_logout)
    app.post("/api/v1/decode-gs1")(_v1_decode_gs1)
    app.post("/api/v1/preview-palette")(_v1_preview_palette)
    app.post("/api/v1/print-palette")(_v1_print_palette)
    app.post("/api/v1/labels/{label_id}/archive")(_v1_archive_label)
    app.post("/api/v1/labels/{label_id}/reprint")(_v1_reprint_label)
    app.get("/api/v1/today-labels")(_v1_today_labels)
    app.get("/api/v1/home-summary")(_v1_home_summary)
    app.get("/api/v1/sscc-log")(_v1_sscc_log)
    # Chargement camion (ramasse) — workflow J1 prévisionnel + J2 chargement
    app.get("/api/v1/cold-room-palettes")(_v1_cold_room_palettes)
    app.get("/api/v1/last-packaging")(_v1_last_packaging)
    app.get("/api/v1/active-ramasses")(_v1_active_ramasses)
    app.post("/api/v1/loadings/previsionnel")(_v1_create_previsionnel)
    app.get("/api/v1/loadings/{ramasse_id}")(_v1_get_loading)
    app.post("/api/v1/loadings/{ramasse_id}/scan")(_v1_scan_palette_to_loading)
    app.post("/api/v1/loadings/{ramasse_id}/finalize")(_v1_finalize_loading)
    app.delete("/api/v1/loadings/{ramasse_id}/palettes/{sscc}")(_v1_unlink_palette)
    # Historique ramasses (read-only) + actions courantes
    app.get("/api/v1/ramasses")(_v1_list_ramasses)
    app.get("/api/v1/ramasses/{ramasse_id}/pdf")(_v1_ramasse_pdf)
    app.post("/api/v1/ramasses/{ramasse_id}/mark-driver-passed")(_v1_mark_driver_passed)
    # Fiches de production (admin only, beta — Sprint 1 : create + list)
    app.post("/api/v1/admin/production-sheets")(_v1_create_production_sheet)
    app.get("/api/v1/admin/production-sheets")(_v1_list_production_sheets)
    # Pre-fill via SSCC (Sprint 2 : sélection brassin + agrégation conditionnement)
    app.get("/api/v1/admin/brassins-en-cours")(_v1_admin_brassins_en_cours)
    app.get("/api/v1/admin/conditionnement-by-lot")(_v1_admin_conditionnement_by_lot)
    # Détail + PATCH partiel (Sprint 3a : auto-save iOS)
    app.get("/api/v1/admin/production-sheets/{sheet_id}")(_v1_get_production_sheet)
    app.patch("/api/v1/admin/production-sheets/{sheet_id}")(_v1_patch_production_sheet)
    # Sprint 3b1 : liste = brassins EB en cours + détail brassin proxy
    app.get("/api/v1/admin/production-overview")(_v1_production_overview)
    app.get("/api/v1/admin/easybeer/brassin/{id_brassin:int}")(_v1_easybeer_brassin_detail)
    # Sprint 4 : finalisation + téléchargement PDF stocké
    app.post("/api/v1/admin/production-sheets/{sheet_id}/finalize")(_v1_finalize_production_sheet)
    app.get("/api/v1/admin/production-sheets/{sheet_id}/pdf")(_v1_get_production_sheet_pdf)
