from __future__ import annotations

import asyncio
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from common.mobile_v1 import (
    _forbidden,
    _resolve_mobile_user,
    _unauthorized,
)

_log = logging.getLogger("ferment.mobile_v1")


# ─── Photos (OVH Object Storage) ──────────────────────────────────────────

_MAX_PHOTO_BYTES = 8 * 1024 * 1024  # 8 Mo, marge confortable au-dessus des 400 Ko habituels
_ALLOWED_PHOTO_TYPES = (
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic",
)


async def _v1_upload_photo(request: Request):
    """Upload une photo dans OVH Object Storage et retourne sa clé + URL signée.

    Body : multipart/form-data avec :
    - ``file`` (required) : image binaire
    - ``sheet_id`` (required) : id de la fiche production à laquelle rattacher

    Retour 200 :
    ``{"key": "production/photos/tenant_id/2026-05-23/sheet_id/abc.jpg",
       "url": "https://...?X-Amz-Signature=...",
       "expires_in": 3600}``

    Le client iOS doit stocker ``key`` (et pas l'URL qui expire) puis appeler
    ``/api/v1/photos/{key}/presigned-url`` pour rafraîchir l'URL.
    """
    user = await _resolve_mobile_user(request)
    if not user:
        return _unauthorized()

    tenant_id = user["tenant_id"]

    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "Invalid form data"}, status_code=400)

    sheet_id = (form.get("sheet_id") or "").strip()
    if not sheet_id:
        return JSONResponse({"error": "Missing 'sheet_id' field"}, status_code=400)

    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "Missing 'file' field"}, status_code=400)

    content_type = (getattr(upload, "content_type", "") or "image/jpeg").lower()
    if content_type.split(";")[0].strip() not in _ALLOWED_PHOTO_TYPES:
        return JSONResponse(
            {"error": f"Type non supporté ({content_type})"},
            status_code=415,
        )

    try:
        image_bytes = await upload.read()
    except Exception:
        _log.exception("Erreur lecture upload photo")
        return JSONResponse({"error": "Cannot read file"}, status_code=400)

    if not image_bytes:
        return JSONResponse({"error": "Empty file"}, status_code=400)
    if len(image_bytes) > _MAX_PHOTO_BYTES:
        return JSONResponse(
            {
                "error": (
                    f"File too large ({len(image_bytes) // 1024} Ko > "
                    f"{_MAX_PHOTO_BYTES // 1024} Ko)"
                ),
            },
            status_code=413,
        )

    from common.object_storage import OVHStorageError, get_presigned_url, upload_photo

    try:
        key = await asyncio.to_thread(
            upload_photo,
            image_bytes,
            tenant_id=tenant_id,
            sheet_id=sheet_id,
            content_type=content_type,
        )
    except OVHStorageError as exc:
        _log.error("Photo upload échec : %s", exc)
        return JSONResponse(
            {"error": f"Storage error: {exc}"}, status_code=503,
        )

    ttl = 3600
    try:
        url = await asyncio.to_thread(get_presigned_url, key, ttl_seconds=ttl)
    except OVHStorageError as exc:
        _log.error("Photo presigned URL échec : %s", exc)
        # L'upload a réussi mais on ne peut pas générer l'URL — retourne juste la clé
        return JSONResponse({"key": key, "expires_in": 0, "url_error": str(exc)})

    return JSONResponse({"key": key, "url": url, "expires_in": ttl})


async def _v1_photo_presigned_url(request: Request, key: str):
    """Génère une nouvelle URL signée pour une photo existante.

    Le client iOS doit appeler ça quand il a besoin d'afficher une photo
    dont l'URL précédente a expiré (TTL 1h par défaut).

    Le path param ``key`` doit être URL-encoded car il contient des ``/``.
    """
    user = await _resolve_mobile_user(request)
    if not user:
        return _unauthorized()

    # Sécurité : la clé doit appartenir au tenant courant
    tenant_id = user["tenant_id"]
    expected_prefix = f"production/photos/{tenant_id}/"
    if not key.startswith(expected_prefix):
        # Note : on cache le tenant_id réel dans l'erreur pour ne pas leaker
        return _forbidden("Key does not belong to your tenant")

    from common.object_storage import OVHStorageError, get_presigned_url

    ttl = 3600
    try:
        url = await asyncio.to_thread(get_presigned_url, key, ttl_seconds=ttl)
    except OVHStorageError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)

    return JSONResponse({"key": key, "url": url, "expires_in": ttl})
