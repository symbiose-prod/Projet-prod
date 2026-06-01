from __future__ import annotations

import asyncio
import datetime as _dt_local
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from common.mobile_v1 import (
    _resolve_mobile_user,
    _scrub_email,
    _unauthorized,
)
from common.mobile_v1.labels import (
    _DEFAULT_DESTINATAIRE,
    _lookup_result_to_dict,
    _palette_to_dict,
)

_log = logging.getLogger("ferment.mobile_v1")


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
        result["email_sent"], user["tenant_id"], _scrub_email(user.get("email")),
    )
    return JSONResponse(result)


async def _v1_create_retroactive(request: Request):
    """Crée un BL « a posteriori » pour une ramasse non scannée.

    Cas d'usage : la douchette n'a pas scanné les palettes le jour de la
    ramasse, le camion est parti mais les palettes restent en chambre froide.
    L'opérateur sélectionne manuellement les SSCC réellement partis → on les
    lie à une ramasse datée du jour passé et on génère un BL marqué « établi
    a posteriori ». AUCUN email n'est envoyé (l'opérateur partage le PDF
    lui-même depuis l'app).

    Body JSON :
      ``{"date_ramasse": "YYYY-MM-DD",
         "sscc_list": ["...", ...],            # palettes sélectionnées en CF
         "destinataire": "SOFRIPA"}``  # optionnel, défaut SOFRIPA

    Retour 200 : PDF binaire (Content-Type: application/pdf). Headers :
      - ``X-Ramasse-Id`` / ``X-Total-Palettes`` / ``X-Total-Cartons``
      - ``X-Total-Poids-Kg`` / ``X-Conflicts`` (SSCC ignorés, séparés par ',')
    Retour 400 : body malformé. Retour 409 : verrou métier (ramasse active
    déjà ouverte) ou aucune palette valide.
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
    destinataire = str(body.get("destinataire") or _DEFAULT_DESTINATAIRE).strip()

    if not isinstance(sscc_list_raw, list) or not sscc_list_raw:
        return JSONResponse(
            {"error": "'sscc_list' must be a non-empty list"}, status_code=400,
        )
    if not date_ramasse_str:
        return JSONResponse({"error": "Missing 'date_ramasse'"}, status_code=400)
    try:
        date_ramasse = _dt_local.date.fromisoformat(date_ramasse_str[:10])
    except ValueError:
        return JSONResponse(
            {"error": "Invalid date_ramasse format (expected YYYY-MM-DD)"},
            status_code=400,
        )

    sscc_list = [str(s).strip() for s in sscc_list_raw if str(s).strip()]

    from common.services.loading_service import create_retroactive_ramasse

    try:
        info, pdf_bytes = await asyncio.to_thread(
            create_retroactive_ramasse,
            user["tenant_id"],
            user_id=user["id"],
            user_email=user.get("email") or "",
            destinataire=destinataire,
            date_ramasse=date_ramasse,
            sscc_list=sscc_list,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception:
        _log.exception("Échec création ramasse rétroactive (mobile)")
        return JSONResponse(
            {"error": "Failed to create retroactive loading"}, status_code=500,
        )

    _log.info(
        "retroactive-loading : ramasse=%s dest=%s palettes=%d conflicts=%d "
        "tenant=%s user=%s",
        info["id"], destinataire, info["total_palettes"],
        len(info["conflicts"]), user["tenant_id"], _scrub_email(user.get("email")),
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="BL_APosteriori_{info["id"][:8]}.pdf"'
            ),
            "X-Ramasse-Id": str(info["id"]),
            "X-Total-Palettes": str(info["total_palettes"]),
            "X-Total-Cartons": str(info["total_cartons"]),
            "X-Total-Poids-Kg": str(info["total_poids_kg"]),
            "X-Conflicts": ",".join(info["conflicts"]),
        },
    )


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
        result.palette.sscc, ramasse_id, user["tenant_id"], _scrub_email(user.get("email")),
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
        info["email_sent"], user["tenant_id"], _scrub_email(user.get("email")),
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
         "total_cartons": M, "total_poids_kg": P,
         "driver_passed": bool, "driver_passed_at": "..."|null}``.
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
    # SSCC annoncés au J1 (workflow informatif) — peut être None pour les
    # ramasses créées avant le refacto 2026-05 (auront previsionnel_sscc_list
    # NULL en DB) ou pour les ramasses définitives (snapshot devenu obsolète).
    raw_psl = ramasse.get("previsionnel_sscc_list")
    previsionnel_sscc_list: list[str] = (
        [str(s) for s in raw_psl] if isinstance(raw_psl, list) else []
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
        # État livraison : indispensable pour que le détail iOS verrouille le
        # bouton "chauffeur passé" quand c'est déjà fait (sinon l'opérateur
        # peut re-marquer une ramasse déjà livrée depuis l'historique).
        "driver_passed": bool(ramasse.get("driver_passed")),
        "driver_passed_at": (
            ramasse["driver_passed_at"].isoformat()
            if ramasse.get("driver_passed_at") else None
        ),
        "palettes": [_palette_to_dict(p) for p in palettes],
        "previsionnel_sscc_list": previsionnel_sscc_list,
        "previsionnel_count": len(previsionnel_sscc_list),
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
        ramasse_id, status, user["tenant_id"], _scrub_email(user.get("email")),
    )

    # Audit : qui a téléchargé quel BL, quand. Traçabilité métier sur les
    # documents officiels envoyés à SOFRIPA.
    from common.audit import ACTION_PDF_DOWNLOADED, log_event
    log_event(
        tenant_id=user["tenant_id"],
        user_email=user.get("email") or None,
        action=ACTION_PDF_DOWNLOADED,
        details={
            "type": "ramasse_bl",
            "ramasse_id": ramasse_id,
            "status": status,
            "filename": fname,
        },
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
        ramasse_id, changed, user["tenant_id"], _scrub_email(user.get("email")),
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
        sscc, ramasse_id, user["tenant_id"], _scrub_email(user.get("email")),
    )
    return JSONResponse({"ok": True})


# ─── Synchro temps réel (SSE) ──────────────────────────────────────────────

async def _v1_events_loadings(request: Request):
    """Stream SSE des events ramasse (palette linked/unlinked, loading
    created/finalized).

    Auth : Bearer token. Le stream est scoped au ``tenant_id`` du user —
    aucun risque de cross-tenant leak.

    Format SSE standard (consommable par EventSource JS / URLSession iOS) :
        event: palette_linked
        data: {"type": "palette_linked", "ramasse_id": "...", "sscc": "...", ...}

        event: palette_unlinked
        data: {...}

    Events possibles :
      - ``palette_linked`` : scan d'une palette CF → camion (ou batch add)
      - ``palette_unlinked`` : retrait d'une palette d'une ramasse
      - ``loading_created`` : nouvelle ramasse ``previsionnel`` créée
      - ``loading_finalized`` : ramasse passée en ``definitif``

    Heartbeat (commentaire ``: ping``) toutes les 25 sec pour éviter que
    les proxies (Caddy/nginx) coupent la connexion inactive.

    Le client doit gérer la reconnexion auto (gérée nativement par
    ``EventSource`` côté navigateur, à coder pour ``URLSession`` iOS).
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    from common.services.realtime import sse_stream

    tenant_id = str(user["tenant_id"])

    return StreamingResponse(
        sse_stream(tenant_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Désactive le buffering nginx/Caddy — critique pour que les
            # chunks arrivent au client immédiatement.
            "X-Accel-Buffering": "no",
        },
    )
