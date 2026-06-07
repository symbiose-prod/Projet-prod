"""Endpoints mobiles d'export XLSX (admin only).

  - POST /api/v1/ramasses/export   → palettes d'une sélection de ramasses
  - GET  /api/v1/sscc-log/export   → journal SSCC selon les filtres

Format commun : un classeur .xlsx, 1 ligne par palette (SSCC), de sorte que
chaque palette soit identifiable avec sa ramasse d'appartenance. Réservé aux
admins (même périmètre que le journal SSCC, qui expose le détail palette).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from common.mobile_v1 import (
    _forbidden,
    _resolve_mobile_user,
    _unauthorized,
)

_log = logging.getLogger("ferment.mobile_v1")

_XLSX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
# Garde-fou : nombre max de ramasses exportables en une fois.
_MAX_RAMASSES = 200


def _require_admin(user: dict | None):
    if user is None:
        return _unauthorized()
    if (user.get("role") or "").lower() != "admin":
        return _forbidden("Admin access required")
    return None


def _xlsx_response(data: bytes, filename: str) -> Response:
    return Response(
        content=data,
        media_type=_XLSX_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Row-Source": "palettes",
        },
    )


def _parse_date(s: str | None) -> _dt.date | None:
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


async def _v1_export_ramasses(request: Request):
    """Export XLSX des palettes d'une sélection de ramasses. ADMIN only.

    Body JSON : ``{"ramasse_ids": ["uuid", ...]}``.

    Retour 200 : binaire .xlsx (1 ligne par palette).
    Retour 400 : sélection vide ou trop grande.
    """
    user = await _resolve_mobile_user(request)
    err = _require_admin(user)
    if err is not None:
        return err

    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_ids = (body or {}).get("ramasse_ids") or []
    if not isinstance(raw_ids, list):
        raw_ids = []
    ids = [str(x).strip() for x in raw_ids if str(x).strip()]
    if not ids:
        return JSONResponse(
            {"error": "Aucune ramasse sélectionnée."}, status_code=400,
        )
    if len(ids) > _MAX_RAMASSES:
        return JSONResponse(
            {"error": f"Trop de ramasses (max {_MAX_RAMASSES})."},
            status_code=400,
        )

    from common.services.sscc_service import list_sscc_for_ramasses
    from common.xlsx_export import build_palettes_xlsx

    try:
        entries = await asyncio.to_thread(
            list_sscc_for_ramasses, user["tenant_id"], ids,
        )
        data = await asyncio.to_thread(
            build_palettes_xlsx, entries, sheet_title="Ramasses",
        )
    except Exception:
        _log.exception("Échec export ramasses (mobile)")
        return JSONResponse({"error": "Export failed"}, status_code=500)

    today = _dt.date.today().strftime("%Y%m%d")
    fname = f"ramasses_{today}.xlsx"
    return _xlsx_response(data, fname)


async def _v1_export_sscc_log(request: Request):
    """Export XLSX du journal SSCC selon les filtres. ADMIN only.

    Query params : ``date_from``, ``date_to`` (YYYY-MM-DD), ``lot`` (ILIKE).
    Mêmes filtres que ``GET /api/v1/sscc-log``.

    Retour 200 : binaire .xlsx (1 ligne par palette).
    """
    user = await _resolve_mobile_user(request)
    err = _require_admin(user)
    if err is not None:
        return err

    params = request.query_params
    date_from = _parse_date(params.get("date_from"))
    date_to = _parse_date(params.get("date_to"))
    lot_filter = (params.get("lot") or "").strip()

    from common.services.sscc_service import list_sscc_log
    from common.xlsx_export import build_palettes_xlsx

    try:
        entries = await asyncio.to_thread(
            list_sscc_log,
            user["tenant_id"],
            date_from=date_from,
            date_to=date_to,
            lot_filter=lot_filter,
            limit=5000,
        )
        data = await asyncio.to_thread(
            build_palettes_xlsx, entries, sheet_title="Journal SSCC",
        )
    except Exception:
        _log.exception("Échec export journal SSCC (mobile)")
        return JSONResponse({"error": "Export failed"}, status_code=500)

    today = _dt.date.today().strftime("%Y%m%d")
    fname = f"journal_sscc_{today}.xlsx"
    return _xlsx_response(data, fname)
