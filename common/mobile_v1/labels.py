from __future__ import annotations

import asyncio
import datetime as _dt_local
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from common.mobile_v1 import (
    _forbidden,
    _resolve_mobile_user,
    _scrub_email,
    _unauthorized,
)

_log = logging.getLogger("ferment.mobile_v1")


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
        _scrub_email(user.get("email")),
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
        ean, case_count, _scrub_email(user.get("email")),
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
        ean, case_count, n_copies, sscc, label_id, _scrub_email(user.get("email")),
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
        label_id, archived_at is not None, user["tenant_id"], _scrub_email(user.get("email")),
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
        label_id, user["tenant_id"], _scrub_email(user.get("email")),
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
