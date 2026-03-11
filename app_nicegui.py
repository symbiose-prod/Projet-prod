#!/usr/bin/env python3
"""
app_nicegui.py
==============
Point d'entrée NiceGUI — Ferment Station.

Lance avec :  python3 app_nicegui.py
"""
from __future__ import annotations

import logging as _logging
import logging.config as _logging_config
import os
import time as _time
import uuid as _uuid

# ─── Chargement .env (python-dotenv, ne surcharge pas les vars existantes) ───
from pathlib import Path

from dotenv import load_dotenv
from nicegui import app, ui
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse

_env_file = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_file, override=False)


# ─── Logging structuré ──────────────────────────────────────────────────────

_IS_PRODUCTION = os.environ.get("ENV") == "production"

if _IS_PRODUCTION:
    _logging_config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": "logging.Formatter",
                "format": '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
                "datefmt": "%Y-%m-%dT%H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "stream": "ext://sys.stdout",
            },
        },
        "root": {"level": "INFO", "handlers": ["console"]},
    })

_log = _logging.getLogger("ferment.auth")
_log_http = _logging.getLogger("ferment.http")

# ─── Fichiers statiques PWA (icônes, manifest, service-worker) ───────────────
app.add_static_files("/static", Path(__file__).resolve().parent / "static")

# Pages publiques (pas besoin d'etre connecte)
PUBLIC_PATHS = {"/login", "/_nicegui", "/favicon.ico", "/reset", "/health", "/static", "/service-worker.js", "/api/sync"}

# Cookie remember-me : duree par defaut (30 jours)
_REMEMBER_MAX_AGE = 30 * 86400


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # ── Logout endpoint : revoque token + clear cookie + redirect ──
        if path == "/api/logout":
            return self._handle_logout(request)

        # Laisser passer les assets NiceGUI et les pages publiques
        if any(path.startswith(p) for p in PUBLIC_PATHS):
            response = await call_next(request)
            self._add_security_headers(response)
            return response

        # Verifier l'authentification cote storage
        user_store = app.storage.user
        if not user_store.get("authenticated"):
            # Tentative de restauration via cookie "Se souvenir de moi"
            fs_token = request.cookies.get("fs_session")
            if fs_token:
                try:
                    from common.auth import verify_session_token
                    remembered = verify_session_token(fs_token)
                    if remembered:
                        user_store.update({
                            "authenticated": True,
                            "id": remembered["id"],
                            "tenant_id": remembered["tenant_id"],
                            "email": remembered["email"],
                            "role": remembered["role"],
                        })
                        _log.info("Session restauree via remember-me pour %s", remembered["email"])
                    else:
                        return RedirectResponse(url="/login")
                except (SQLAlchemyError, OSError, ValueError):
                    _log.warning("Erreur verification remember-me token", exc_info=True)
                    return RedirectResponse(url="/login")
            else:
                return RedirectResponse(url="/login")

        # Validation serveur periodique (toutes les 5 min max)
        import time
        now = time.time()
        last_check = user_store.get("_server_validated_at", 0)
        if now - last_check > 300:  # 5 minutes
            try:
                from common.auth import find_user_by_email
                user_email = user_store.get("email", "")
                db_user = find_user_by_email(user_email) if user_email else None
                if not db_user or not db_user.get("is_active"):
                    _log.warning("Session invalidee : user %s introuvable ou desactive", user_email)
                    user_store.clear()
                    return RedirectResponse(url="/login")
                # Resync tenant_id (protection contre falsification cote client)
                user_store["tenant_id"] = str(db_user["tenant_id"])
                user_store["role"] = db_user.get("role", "user")
                user_store["_server_validated_at"] = now
            except (SQLAlchemyError, OSError):
                _log.exception("Erreur validation session serveur")
                # Grace period : si la derniere validation reussie date de
                # moins de 30 min, on laisse passer temporairement.
                _GRACE_SECONDS = 1800  # 30 min
                if last_check == 0 or (now - last_check) > _GRACE_SECONDS:
                    _log.warning(
                        "Grace period expiree (DB down), deconnexion de %s",
                        user_store.get("email"),
                    )
                    user_store.clear()
                    return RedirectResponse(url="/login")

        # ── Process request ──
        response = await call_next(request)

        # ── Headers de sécurité ──
        self._add_security_headers(response)

        # ── Poser le cookie remember-me HttpOnly si pending ──
        try:
            pending_token = user_store.get("_pending_remember_token")
            if pending_token:
                try:
                    del user_store["_pending_remember_token"]
                except KeyError:
                    pass
                _is_prod = os.environ.get("ENV") == "production"
                response.set_cookie(
                    "fs_session",
                    pending_token,
                    max_age=_REMEMBER_MAX_AGE,
                    path="/",
                    httponly=True,
                    secure=_is_prod,
                    samesite="lax",
                )
        except (KeyError, TypeError, RuntimeError):
            _log.warning("Erreur pose cookie remember-me", exc_info=True)

        return response

    @staticmethod
    def _add_security_headers(response) -> None:
        """Ajoute les headers de sécurité sur toutes les réponses (publiques et authentifiées)."""
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # CSP : NiceGUI nécessite 'unsafe-inline' + 'unsafe-eval' pour Quasar/Vue3 + WebSocket
        # Google Fonts (Inter) : fonts.googleapis.com (CSS) + fonts.gstatic.com (woff2)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "img-src 'self' data:; "
            "connect-src 'self' wss: ws:; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "frame-ancestors 'none'"
        )
        if _IS_PRODUCTION:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    @staticmethod
    def _handle_logout(request: Request) -> RedirectResponse:
        """Logout: revoque le token DB + vide la session NiceGUI + supprime le cookie."""
        fs_token = request.cookies.get("fs_session")
        if fs_token:
            try:
                from common.auth import revoke_session_token
                revoke_session_token(fs_token)
            except (SQLAlchemyError, OSError):
                _log.warning("Erreur revocation token logout", exc_info=True)
        # Vider le storage NiceGUI (supprime authenticated, tenant_id, etc.)
        try:
            app.storage.user.clear()
        except (KeyError, RuntimeError):
            _log.debug("Impossible de vider storage user au logout", exc_info=True)
        resp = RedirectResponse(url="/login", status_code=302)
        resp.delete_cookie("fs_session", path="/")
        return resp


app.add_middleware(AuthMiddleware)


# ─── Request logging middleware ──────────────────────────────────────────────

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logue méthode, path, statut et durée de chaque requête HTTP — avec request_id."""

    async def dispatch(self, request: Request, call_next):
        request_id = str(_uuid.uuid4())[:8]
        request.state.request_id = request_id
        start = _time.monotonic()
        response = await call_next(request)
        duration_ms = (_time.monotonic() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        path = request.url.path
        # Ignorer les assets statiques NiceGUI (trop bruyant)
        if not path.startswith("/_nicegui"):
            _log_http.info(
                "[%s] %s %s → %d (%.0fms)",
                request_id, request.method, path, response.status_code, duration_ms,
            )
        return response


app.add_middleware(RequestLoggingMiddleware)


# ─── Import des pages (les @ui.page sont enregistrés à l'import) ────────────

from nicegui import ui  # noqa: F811 — restaure nicegui.ui après imports locaux ui.*

import ui.accueil  # noqa: F401 — /accueil
import ui.auth  # noqa: F401 — /login, /reset/{token}
import ui.production  # noqa: F401 — /production
import ui.ramasse  # noqa: F401 — /ramasse
import ui.ressources  # noqa: F401 — /ressources
import ui.stocks  # noqa: F401 — /stocks

from nicegui import ui  # noqa: F811 — re-restaure nicegui.ui après imports locaux ui.*

# ─── Health check ────────────────────────────────────────────────────────────

@app.get("/health")
async def _health_check():
    """Endpoint de santé enrichi : DB + espace disque."""
    import shutil

    checks: dict[str, str] = {}

    # 1. Database
    from db.conn import ping
    db_ok, db_msg = ping()
    checks["db"] = "ok" if db_ok else db_msg

    # 2. Espace disque (> 100 MB requis)
    try:
        usage = shutil.disk_usage("/")
        free_mb = usage.free / (1024 * 1024)
        checks["disk"] = "ok" if free_mb > 100 else f"low ({free_mb:.0f} MB)"
    except OSError:
        checks["disk"] = "error"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if all_ok else "degraded", "checks": checks},
        status_code=200 if all_ok else 503,
    )


# ─── API Sync étiquettes (/api/sync/*) ───────────────────────────────────────
# Routes publiques (bypass AuthMiddleware) avec auth par clé API Bearer token.

_sync_log = _logging.getLogger("ferment.sync.api")


def _extract_bearer_key(request: Request) -> str | None:
    """Extrait le token Bearer depuis l'en-tête Authorization."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def _verify_sync_auth(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """Vérifie la clé API sync. Retourne (auth_info, None) ou (None, error_response)."""
    from common.sync.api_key import verify_api_key

    raw_key = _extract_bearer_key(request)
    if not raw_key:
        return None, JSONResponse({"error": "Missing Authorization header"}, status_code=401)
    auth_info = verify_api_key(raw_key)
    if not auth_info:
        return None, JSONResponse({"error": "Invalid API key"}, status_code=401)
    return auth_info, None


@app.get("/api/sync/pending")
async def _sync_pending(request: Request):
    """Agent Windows : récupère la dernière opération pending."""
    auth_info, err = _verify_sync_auth(request)
    if err:
        return err

    from db.conn import run_sql
    import json

    rows = run_sql(
        """SELECT id, op_type, payload, product_count, created_at
           FROM sync_operations
           WHERE tenant_id = :t AND status = 'pending'
           ORDER BY created_at DESC
           LIMIT 1""",
        {"t": auth_info["tenant_id"]},
    )
    if not rows:
        return JSONResponse(status_code=204, content=None)

    op = rows[0]
    # Passer en status "fetched"
    run_sql(
        "UPDATE sync_operations SET status = 'fetched', fetched_at = now() WHERE id = :id",
        {"id": op["id"]},
    )
    _sync_log.info("Op #%s fetched by agent (tenant %s)", op["id"], auth_info["tenant_id"])

    payload = op["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)

    return JSONResponse({
        "operation_id": op["id"],
        "op_type": op["op_type"],
        "product_count": op["product_count"],
        "created_at": op["created_at"].isoformat() if hasattr(op["created_at"], "isoformat") else str(op["created_at"]),
        "products": payload,
    })


@app.post("/api/sync/ack")
async def _sync_ack(request: Request):
    """Agent Windows : confirme le traitement d'une opération."""
    auth_info, err = _verify_sync_auth(request)
    if err:
        return err

    from db.conn import run_sql

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    op_id = body.get("operation_id")
    status = body.get("status")
    error_msg = body.get("error_msg", "")

    if not op_id or status not in ("applied", "error"):
        return JSONResponse(
            {"error": "Required: operation_id (int) + status ('applied'|'error')"},
            status_code=400,
        )

    count = run_sql(
        """UPDATE sync_operations
           SET status = :s, applied_at = now(), error_msg = :e
           WHERE id = :id AND tenant_id = :t""",
        {"s": status, "e": error_msg or None, "id": op_id, "t": auth_info["tenant_id"]},
    )
    if not count:
        return JSONResponse({"error": "Operation not found"}, status_code=404)

    _sync_log.info("Op #%s ack: %s %s", op_id, status, f"({error_msg})" if error_msg else "")
    return JSONResponse({"ok": True})


@app.post("/api/sync/trigger")
async def _sync_trigger(request: Request):
    """Déclenchement manuel depuis l'UI NiceGUI (auth session, pas API key).

    Cet endpoint est dans PUBLIC_PATHS (/api/sync) mais on vérifie manuellement
    soit un Bearer token (API key), soit une session NiceGUI authentifiée.
    """
    # Essayer d'abord l'auth API key (pour les appels programmatiques)
    raw_key = _extract_bearer_key(request)
    if raw_key:
        from common.sync.api_key import verify_api_key
        auth_info = verify_api_key(raw_key)
        if not auth_info:
            return JSONResponse({"error": "Invalid API key"}, status_code=401)
        tenant_id = auth_info["tenant_id"]
    else:
        # Fallback : auth session NiceGUI
        user_store = app.storage.user
        if not user_store.get("authenticated"):
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        tenant_id = user_store.get("tenant_id")

    if not tenant_id:
        return JSONResponse({"error": "No tenant context"}, status_code=400)

    import asyncio
    from common.sync.collector import collect_label_data
    from common.sync import create_sync_operation

    try:
        loop = asyncio.get_event_loop()
        products = await loop.run_in_executor(None, collect_label_data)
        if not products:
            return JSONResponse({"operation_id": None, "product_count": 0, "status": "empty"})

        op = create_sync_operation(products, tenant_id=tenant_id, triggered_by="manual")
        return JSONResponse({
            "operation_id": op["id"],
            "product_count": op["product_count"],
            "status": "pending",
        })
    except Exception:
        _sync_log.exception("Erreur sync trigger manuelle")
        return JSONResponse({"error": "Sync failed"}, status_code=500)


# ─── Nettoyage périodique (sessions / resets expirés) ────────────────────────

_CLEANUP_INTERVAL = 3600  # 1 heure


def _do_cleanup() -> None:
    """Purge les sessions, tokens et lockouts expirés."""
    try:
        from common.auth import cleanup_expired_failures, cleanup_expired_resets, cleanup_expired_sessions
        cleanup_expired_sessions()
        cleanup_expired_resets()
        cleanup_expired_failures()
        _log.debug("Nettoyage périodique OK")
    except (SQLAlchemyError, OSError):
        _log.exception("Erreur nettoyage sessions/resets")


async def _periodic_cleanup() -> None:
    """Boucle infinie : relance le nettoyage toutes les _CLEANUP_INTERVAL secondes."""
    import asyncio
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL)
        _do_cleanup()


@app.on_startup
async def _startup_cleanup():
    """Vérifications de sécurité + nettoyage initial + démarrage du timer périodique."""
    import asyncio

    # ── Vérification : ALLOWED_TENANTS obligatoire en production ──
    if os.environ.get("ENV") == "production":
        if not os.environ.get("ALLOWED_TENANTS", "").strip():
            raise RuntimeError(
                "ALLOWED_TENANTS manquant en production — n'importe qui pourrait créer un tenant.\n"
                "Définissez ALLOWED_TENANTS dans le .env (ex: ALLOWED_TENANTS=Symbiose Kéfir)"
            )

    # Nettoyage initial
    _do_cleanup()

    # Démarrer le timer périodique (toutes les heures)
    asyncio.ensure_future(_periodic_cleanup())

    # Démarrer le scheduler sync étiquettes (tous les jours à 12h Paris)
    from common.sync.scheduler import daily_sync_loop
    asyncio.ensure_future(daily_sync_loop())


# ─── Service Worker (servi depuis / pour scope racine) ──────────────────────

@app.get("/service-worker.js")
async def _service_worker():
    """Sert le service worker depuis la racine pour avoir le scope '/'."""
    return FileResponse(
        Path(__file__).resolve().parent / "static" / "service-worker.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# ─── Redirect racine ────────────────────────────────────────────────────────

@ui.page("/")
def root():
    ui.navigate.to("/accueil")


# ─── Lancement ──────────────────────────────────────────────────────────────

def _get_storage_secret() -> str:
    """Exige un vrai secret pour signer les cookies de session."""
    secret = os.environ.get("NICEGUI_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "NICEGUI_SECRET manquant — génère-en un :\n"
            '  python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    return secret


if __name__ in {"__main__", "__mp_main__"}:
    port = int(os.environ.get("NICEGUI_PORT", "8502"))
    ui.run(
        title="Ferment Station",
        port=port,
        show=False,
        reload=os.environ.get("ENV") != "production",
        favicon=Path(__file__).resolve().parent / "static" / "icons" / "favicon-32.png",
        dark=False,
        language="fr",
        storage_secret=_get_storage_secret(),
    )
