"""
common/mobile_v1.py
===================
Routes ``/api/v1/*`` pour l'app iOS native "Ferment station".

Ce module regroupe TOUS les endpoints destinés au client mobile :

| Méthode | Route                            | Auth | Rôle                                       |
|---------|----------------------------------|------|--------------------------------------------|
| POST    | ``/api/v1/auth/login``           | —    | email+password → token Bearer              |
| POST    | ``/api/v1/auth/logout``          | Tk   | révoque le token courant                   |
| DELETE  | ``/api/v1/auth/me``              | Tk   | suppression compte (RGPD art.17, Apple iOS 14.5+) |
| GET     | ``/api/v1/auth/devices``         | Tk   | liste les appareils (tokens) du user        |
| DELETE  | ``/api/v1/auth/devices/{id}``    | Tk   | révoque un appareil du user                 |
| POST    | ``/api/v1/decode-gs1``           | Tk   | décode string GS1-128 + lookup produit     |
| POST    | ``/api/v1/print-palette``        | Tk   | génère SSCC + PDF étiquette palette        |
| POST    | ``/api/v1/labels/{id}/archive``  | Tk   | toggle archive (réversible)                |
| POST    | ``/api/v1/labels/{id}/reprint``  | Tk   | régénère le PDF d'une étiquette historisée |
| GET     | ``/api/v1/today-labels``         | Tk   | étiquettes du jour (toutes, archivées incl.)|
| GET     | ``/api/v1/home-summary``         | Tk   | compteurs jour/mois + 20 dernières         |
| GET     | ``/api/v1/sscc-log``             | Tk*  | journal SSCC (admin uniquement)            |
| GET     | ``/api/v1/cold-room-palettes``   | Tk   | palettes étiquetées non encore chargées    |
| GET     | ``/api/v1/last-packaging``       | Tk   | emballages habituels du destinataire + items configurés |
| POST    | ``/api/v1/packaging-request``    | Tk   | demande d'emballages à ramener (sans ramasse, email best-effort) |
| GET     | ``/api/v1/packaging-requests``   | Tk   | demandes d'emballages encore "à recevoir" (non livrées) |
| GET     | ``/api/v1/packaging-requests/history`` | Tk | historique paginé des demandes (avec état livraison) |
| POST    | ``/api/v1/packaging-requests/{id}/mark-delivered`` | Tk | marque une demande comme reçue (audit log) |
| GET     | ``/api/v1/active-ramasses``      | Tk   | ramasses ``previsionnel`` ouvertes (J2 reprise) |
| POST    | ``/api/v1/loadings/previsionnel`` | Tk  | crée + envoie BL prévisionnel (J1 soir)    |
| POST    | ``/api/v1/loadings/retroactive`` | Tk   | BL « a posteriori » (ramasse non scannée), PDF sans email |
| GET     | ``/api/v1/loadings/{id}``        | Tk   | détail d'un chargement (palettes liées + totaux) |
| POST    | ``/api/v1/loadings/{id}/scan``   | Tk   | scan SSCC + lien à la ramasse (chargement J2) |
| POST    | ``/api/v1/loadings/{id}/finalize`` | Tk | finalise prévisionnel → définitif + PDF BL + email |
| DELETE  | ``/api/v1/loadings/{id}/palettes/{sscc}`` | Tk | délie une palette d'une ramasse (soft) |
| GET     | ``/api/v1/events/loadings``      | Tk   | stream SSE temps réel (link/unlink/created/finalized) |
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
| GET     | ``/api/v1/admin/cuves`` | Tk* | registre des cuves + tables de calibration volume↔hauteur |
| POST    | ``/api/v1/admin/production-sheets/{id}/finalize`` | Tk* | finalise (génère PDF + status completed) |
| GET     | ``/api/v1/admin/production-sheets/{id}/pdf`` | Tk* | re-télécharge le PDF stocké |
| GET     | ``/api/v1/admin/users``          | Tk*  | liste les comptes du tenant (gestion des accès) |
| POST    | ``/api/v1/admin/users/invite``   | Tk*  | crée un compte + envoie l'email d'invitation |
| PATCH   | ``/api/v1/admin/users/{id}/role`` | Tk* | change le rôle (user/admin/operateur) |
| PATCH   | ``/api/v1/admin/users/{id}/active`` | Tk* | active / désactive un compte |

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
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from common.mobile_auth import (
    create_mobile_token,
    extract_bearer_token,
    list_mobile_tokens,
    revoke_mobile_token,
    revoke_mobile_token_by_id,
    verify_mobile_token,
)

_log = logging.getLogger("ferment.mobile_v1")


def _scrub_email(email: str | None) -> str:
    """Hash court (8 hex chars) d'un email pour les logs applicatifs.

    Évite la PII dans journalctl/Console.app/Sentry breadcrumbs tout en
    permettant de corréler les events d'un même utilisateur via le même
    hash. À utiliser dans ``_log.info(...)`` partout où on identifiait
    l'opérateur par son email.

    L'email entier reste dans ``audit_log`` (table dédiée traçabilité,
    accès restreint) — c'est uniquement les logs applicatifs verbeux qu'on
    pseudonymise ici.
    """
    if not email:
        return "anonymous"
    import hashlib
    return "u:" + hashlib.sha256(email.encode("utf-8")).hexdigest()[:8]


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


async def _v1_delete_account(request: Request):
    """Suppression compte utilisateur — conforme Apple iOS 14.5+ et RGPD art.17.

    Path : ``DELETE /api/v1/auth/me``.

    Pipeline atomique (`mobile_auth.delete_mobile_user_account`) :
      - users.is_active = false
      - users.email pseudonymisée (`deleted-<uid>@symbiose-internal.local`)
      - users.password_hash vidée (impossible de re-login)
      - tokens mobile_api_tokens du user : tous révoqués
      - audit_log.user_email du tenant correspondant : NULL (anonymisation
        rétrospective ; actions/details conservés pour traçabilité alimentaire)
      - audit_log : nouvel évènement `account_deleted` (anonyme)

    Retour 200 : ``{"ok": true}`` — le client doit immédiatement effacer
    le token côté Keychain et basculer sur LoginView.
    Retour 401 : token invalide / déjà révoqué.
    Retour 500 : erreur DB inattendue.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    from common.mobile_auth import delete_mobile_user_account

    try:
        ok = await asyncio.to_thread(
            delete_mobile_user_account,
            user_id=user["id"],
            tenant_id=user["tenant_id"],
        )
    except Exception:
        _log.exception(
            "Échec suppression compte mobile (user_id=%s)", user["id"],
        )
        return JSONResponse(
            {"error": "Account deletion failed"}, status_code=500,
        )

    if not ok:
        # Compte introuvable ou déjà supprimé — idempotent côté client.
        return JSONResponse({"ok": True})

    _log.info(
        "Compte mobile supprimé via API : tenant=%s",
        user["tenant_id"],
    )
    return JSONResponse({"ok": True})


# ─── Auth : gestion des appareils (« Mes appareils ») ──────────────────────

def _serialize_device(d: dict) -> dict:
    """Sérialise un token mobile en entrée JSON pour l'écran Mes appareils."""
    return {
        "id": d["id"],
        "device_name": d["device_name"],
        "created_at": (
            d["created_at"].isoformat() if d.get("created_at") else None
        ),
        "last_used_at": (
            d["last_used_at"].isoformat() if d.get("last_used_at") else None
        ),
        "expires_at": (
            d["expires_at"].isoformat() if d.get("expires_at") else None
        ),
        "expired": d["expired"],
        "is_current": d["is_current"],
    }


async def _v1_list_devices(request: Request):
    """Liste les appareils (tokens mobiles) de l'utilisateur courant.

    Chaque utilisateur ne voit QUE ses propres appareils. ``is_current``
    marque l'appareil depuis lequel la requête est faite.

    Retour 200 : ``{"devices": [{id, device_name, created_at, last_used_at,
    expires_at, expired, is_current}]}``.
    """
    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    current_token = extract_bearer_token(request.headers.get("authorization"))
    devices = await asyncio.to_thread(
        list_mobile_tokens, user["id"], current_token,
    )
    return JSONResponse({"devices": [_serialize_device(d) for d in devices]})


async def _v1_revoke_device(device_id: str, request: Request):
    """Révoque un appareil (token mobile) de l'utilisateur courant.

    Scopé à l'utilisateur : impossible de révoquer l'appareil d'un autre.

    Retour 200 : ``{"ok": true}``.
    Retour 400 : ``device_id`` mal formé (pas un UUID).
    Retour 404 : appareil introuvable, déjà révoqué, ou hors périmètre user.
    """
    import uuid as _uuid

    user = await _resolve_mobile_user(request)
    if user is None:
        return _unauthorized()

    try:
        _uuid.UUID(str(device_id))
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"error": "Invalid device id"}, status_code=400)

    revoked = await asyncio.to_thread(
        revoke_mobile_token_by_id, user["id"], str(device_id),
    )
    if not revoked:
        return JSONResponse({"error": "Device not found"}, status_code=404)
    _log.info("Appareil révoqué : token=%s user=%s", device_id, user["id"])
    return JSONResponse({"ok": True})


# ─── Sous-modules : endpoints déportés par responsabilité ──────────────────
# Importés APRÈS la définition des helpers ci-dessus pour que, lorsqu'un
# sous-module fait ``from common.mobile_v1 import _resolve_mobile_user``, le
# package partiellement initialisé expose déjà ces symboles (pas d'import
# circulaire).

from common.mobile_v1.admin_users import (
    _v1_admin_invite_user,
    _v1_admin_list_users,
    _v1_admin_set_user_active,
    _v1_admin_set_user_role,
)
from common.mobile_v1.labels import (
    _v1_archive_label,
    _v1_cold_room_palettes,
    _v1_decode_gs1,
    _v1_home_summary,
    _v1_preview_palette,
    _v1_print_palette,
    _v1_reprint_label,
    _v1_sscc_log,
    _v1_today_labels,
)
from common.mobile_v1.packaging import (
    _v1_last_packaging,
    _v1_mark_packaging_request_delivered,
    _v1_packaging_request,
    _v1_packaging_requests_history,
    _v1_pending_packaging_requests,
)
from common.mobile_v1.photos import (
    _v1_photo_presigned_url,
    _v1_upload_photo,
)
from common.mobile_v1.production_sheets import (
    _v1_admin_brassins_en_cours,
    _v1_admin_conditionnement_by_lot,
    _v1_admin_cuves,
    _v1_create_production_sheet,
    _v1_easybeer_brassin_detail,
    _v1_finalize_production_sheet,
    _v1_get_production_sheet,
    _v1_get_production_sheet_pdf,
    _v1_list_production_sheets,
    _v1_patch_production_sheet,
    _v1_production_overview,
)
from common.mobile_v1.ramasse import (
    _v1_active_ramasses,
    _v1_create_previsionnel,
    _v1_create_retroactive,
    _v1_events_loadings,
    _v1_finalize_loading,
    _v1_get_loading,
    _v1_list_ramasses,
    _v1_mark_driver_passed,
    _v1_ramasse_pdf,
    _v1_scan_palette_to_loading,
    _v1_unlink_palette,
)

# ─── Enregistrement des routes (appelé depuis app_nicegui.py) ──────────────

def register_routes(app) -> None:
    """Enregistre les endpoints ``/api/v1/*`` sur l'app NiceGUI/FastAPI.

    Appeler une fois au démarrage depuis ``app_nicegui.py``, après la
    configuration du middleware d'auth.
    """
    app.post("/api/v1/auth/login")(_v1_login)
    app.post("/api/v1/auth/logout")(_v1_logout)
    app.delete("/api/v1/auth/me")(_v1_delete_account)
    app.get("/api/v1/auth/devices")(_v1_list_devices)
    app.delete("/api/v1/auth/devices/{device_id}")(_v1_revoke_device)
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
    app.post("/api/v1/packaging-request")(_v1_packaging_request)
    app.get("/api/v1/packaging-requests")(_v1_pending_packaging_requests)
    app.get(
        "/api/v1/packaging-requests/history",
    )(_v1_packaging_requests_history)
    app.post(
        "/api/v1/packaging-requests/{request_id}/mark-delivered",
    )(_v1_mark_packaging_request_delivered)
    app.get("/api/v1/active-ramasses")(_v1_active_ramasses)
    app.post("/api/v1/loadings/previsionnel")(_v1_create_previsionnel)
    app.post("/api/v1/loadings/retroactive")(_v1_create_retroactive)
    app.get("/api/v1/loadings/{ramasse_id}")(_v1_get_loading)
    app.post("/api/v1/loadings/{ramasse_id}/scan")(_v1_scan_palette_to_loading)
    app.post("/api/v1/loadings/{ramasse_id}/finalize")(_v1_finalize_loading)
    app.delete("/api/v1/loadings/{ramasse_id}/palettes/{sscc}")(_v1_unlink_palette)
    # Stream SSE temps réel (palette linked/unlinked, loading created/finalized).
    # Scope tenant strict ; alimente la synchro multi-comptes web + iOS.
    app.get("/api/v1/events/loadings")(_v1_events_loadings)
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
    app.get("/api/v1/admin/cuves")(_v1_admin_cuves)
    # Sprint 4 : finalisation + téléchargement PDF stocké
    app.post("/api/v1/admin/production-sheets/{sheet_id}/finalize")(_v1_finalize_production_sheet)
    app.get("/api/v1/admin/production-sheets/{sheet_id}/pdf")(_v1_get_production_sheet_pdf)
    # Gestion des accès (admin only) : liste équipe + invitation + rôle + activation
    app.get("/api/v1/admin/users")(_v1_admin_list_users)
    app.post("/api/v1/admin/users/invite")(_v1_admin_invite_user)
    app.patch("/api/v1/admin/users/{user_id}/role")(_v1_admin_set_user_role)
    app.patch("/api/v1/admin/users/{user_id}/active")(_v1_admin_set_user_active)
    # Sprint Photos S3 : upload + URL signée pour photos d'incidents (OVH Object Storage)
    app.post("/api/v1/photos/upload")(_v1_upload_photo)
    app.get("/api/v1/photos/{key:path}/presigned-url")(_v1_photo_presigned_url)
