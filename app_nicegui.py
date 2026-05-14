#!/usr/bin/env python3
"""
app_nicegui.py
==============
Point d'entrée NiceGUI — Ferment Station.

Lance avec :  python3 app_nicegui.py
"""
from __future__ import annotations

import asyncio
import logging as _logging
import logging.config as _logging_config
import os
import time as _time
import uuid as _uuid
from collections import deque

# ─── Chargement .env (python-dotenv, ne surcharge pas les vars existantes) ───
from pathlib import Path

from dotenv import load_dotenv
from nicegui import app, ui
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

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
# /assets : photos produits (pour la page Étiquettes palette, etc.)
app.add_static_files("/assets", Path(__file__).resolve().parent / "assets")

# Pages publiques (pas besoin d'etre connecte)
PUBLIC_PATHS = {
    "/login", "/_nicegui", "/favicon.ico", "/reset",
    "/health", "/metrics", "/static", "/assets",
    "/service-worker.js", "/api/sync",
    # Endpoints de l'agent imprimante (auth via bearer token PRINT_AGENT_TOKEN,
    # pas de session NiceGUI). Le slash trailing évite de matcher le POST
    # utilisateur /api/print-jobs (qui reste protégé par session).
    "/api/print-jobs/",
}

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
                _GRACE_SECONDS = 300  # 5 min
                if last_check == 0 or (now - last_check) > _GRACE_SECONDS:
                    _log.warning(
                        "Grace period expiree (DB down), deconnexion de %s",
                        user_store.get("email"),
                    )
                    user_store.clear()
                    return RedirectResponse(url="/login")

        # ── Hardening multi-tenant : refuser toute session authentifiée
        # sans tenant_id. Protège contre fuite inter-tenant si le storage
        # est corrompu ou manipulé. Attache tenant_id à request.state pour
        # les routes FastAPI (ex: /api/sync) qui ne peuvent pas lire app.storage.
        _tid = user_store.get("tenant_id")
        if not _tid or not str(_tid).strip():
            _log.error(
                "Session authentifiée sans tenant_id — logout forcé pour %s",
                user_store.get("email") or "?",
            )
            user_store.clear()
            return RedirectResponse(url="/login")
        request.state.tenant_id = str(_tid)

        # ── RBAC : vérifie l'accès basé sur le rôle ──
        # L'opérateur n'a accès qu'aux pages listées dans common/permissions.py
        # OPERATEUR_ALLOWED_PATHS. Toute autre URL le redirige vers sa page
        # d'accueil. Admin et user passent partout (admin.py fait son propre
        # check pour les pages strictement admin).
        from common.permissions import can_access_path, home_page_for_role
        _role = (user_store.get("role") or "user").strip().lower()
        if not can_access_path(_role, path):
            _log.warning(
                "Accès refusé : %s (role=%s) → %s — redirect vers home",
                user_store.get("email") or "?", _role, path,
            )
            return RedirectResponse(url=home_page_for_role(_role))

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
                    samesite="strict",
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
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"

    @staticmethod
    def _handle_logout(request: Request) -> RedirectResponse:
        """Logout: revoque le token DB + vide la session NiceGUI + supprime le cookie."""
        # Capturer l'email avant de vider le storage (pour l'audit)
        _logout_email: str | None = None
        _logout_tenant: str | None = None
        try:
            _logout_email = app.storage.user.get("email")
            _logout_tenant = app.storage.user.get("tenant_id")
        except (KeyError, RuntimeError):
            pass

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

        # Audit trail
        if _logout_email:
            try:
                from common.audit import log_event
                log_event(
                    tenant_id=_logout_tenant,
                    user_email=_logout_email,
                    action="logout",
                )
            except Exception:
                _log.debug("Erreur audit logout", exc_info=True)

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
        # ── Alerte email sur erreur serveur (5xx) ──
        if response.status_code >= 500 and not path.startswith("/_nicegui"):
            try:
                from common.error_alerting import send_error_alert
                user_email = None
                try:
                    user_email = app.storage.user.get("email")
                except Exception:
                    pass
                send_error_alert(
                    method=request.method,
                    path=path,
                    status_code=response.status_code,
                    request_id=request_id,
                    user_email=user_email,
                )
            except Exception:
                _log.debug("Erreur envoi alerte 500", exc_info=True)
        return response


app.add_middleware(RequestLoggingMiddleware)


# ─── Import des pages (les @ui.page sont enregistrés à l'import) ────────────

import pages.accueil  # noqa: F401 — /accueil
import pages.admin  # noqa: F401 — /admin (admin only)
import pages.auth  # noqa: F401 — /login, /reset/{token}
import pages.chargement_camion  # noqa: F401 — /chargement-camion
import pages.commercial  # noqa: F401 — /commercial
import pages.etiquettes_palette  # noqa: F401 — /etiquettes-palette
import pages.historique_ramasses  # noqa: F401 — /historique-ramasses
import pages.nomenclatures  # noqa: F401 — /nomenclatures
import pages.previsions  # noqa: F401 — /previsions
import pages.production  # noqa: F401 — /production
import pages.ramasse  # noqa: F401 — /ramasse
import pages.ressources  # noqa: F401 — /ressources
import pages.sscc_log  # noqa: F401 — /sscc-log (admin only)
import pages.stocks  # noqa: F401 — /stocks
import pages.sync  # noqa: F401 — /sync
import pages.tags  # noqa: F401 — /tags

# ─── Health check ────────────────────────────────────────────────────────────

@app.get("/health")
async def _health_check():
    """Endpoint de santé enrichi : DB + disque + état EasyBeer + cache.

    Retour JSON::

        {
            "status": "ok" | "degraded",
            "checks": {
                "db": "ok" | "<error msg>",
                "disk": "ok" | "low (…)",
                "easybeer": {
                    "circuit_breaker": "closed" | "open (45s restantes)",
                    "rate_limit": "ok" | "throttled (3s)",
                    "configured": true | false
                },
                "cache": {"eb_entries": <int>}
            }
        }

    ``status=degraded`` + HTTP 503 si un check essentiel (db, disk) n'est
    pas ok. Les checks EasyBeer et cache sont informatifs uniquement et
    ne dégradent pas le statut (l'app tourne en mode dégradé).
    """
    import shutil
    from typing import Any as _Any

    checks: dict[str, _Any] = {}

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

    # 3. EasyBeer (circuit breaker + rate-limit + configured) — informatif
    try:
        from common.easybeer._client import (
            circuit_breaker_state,
            is_rate_limited,
        )
        from common.easybeer._client import (
            is_configured as eb_configured,
        )
        cb = circuit_breaker_state()
        cb_remaining = int(cb.get("remaining", 0) or 0)
        rl_remaining = int(is_rate_limited() or 0)
        checks["easybeer"] = {
            "configured": eb_configured(),
            "circuit_breaker": (
                "closed" if cb_remaining <= 0
                else f"open ({cb_remaining}s restantes)"
            ),
            "rate_limit": (
                "ok" if rl_remaining <= 0
                else f"throttled ({rl_remaining}s)"
            ),
            "failure_count": int(cb.get("failures", 0) or 0),
        }
    except Exception as exc:
        checks["easybeer"] = {"error": str(exc)[:100]}

    # 4. Cache EasyBeer (DB L2) — informatif
    try:
        from db.conn import run_sql as _run_sql
        rows = _run_sql("SELECT COUNT(*) AS n FROM eb_cache") or []
        checks["cache"] = {"eb_entries": int(rows[0]["n"]) if rows else 0}
    except Exception:
        checks["cache"] = {"eb_entries": -1}

    # Dégradation uniquement sur db/disk (checks bloquants)
    essential_ok = checks.get("db") == "ok" and checks.get("disk") == "ok"
    return JSONResponse(
        {"status": "ok" if essential_ok else "degraded", "checks": checks},
        status_code=200 if essential_ok else 503,
    )


# ─── Metrics Prometheus (text format, sans dépendance externe) ──────────────

@app.get("/metrics")
async def _metrics():
    """Exporte les métriques internes au format Prometheus text-based exposition.

    Format conforme : https://prometheus.io/docs/instrumenting/exposition_formats/

    Métriques exposées :
    - ferment_easybeer_circuit_breaker_open (gauge 0|1)
    - ferment_easybeer_circuit_breaker_failure_count (gauge)
    - ferment_easybeer_rate_limit_remaining_seconds (gauge)
    - ferment_easybeer_configured (gauge 0|1)
    - ferment_eb_cache_entries (gauge)
    - ferment_sync_rate_limit_active_keys (gauge)
    - ferment_sync_rate_limit_hits_total{key_id} (gauge, par clé)
    - ferment_app_info{version, env} (gauge, toujours 1)

    Accessible sans auth (comme /health) — à restreindre par IP côté Caddy
    ou firewall en prod si l'endpoint fuit des infos sensibles.
    """
    from starlette.responses import PlainTextResponse

    lines: list[str] = []
    _emitted_helps: set[str] = set()

    def _emit(name: str, help_text: str, metric_type: str,
              value: float, labels: dict[str, str] | None = None) -> None:
        if name not in _emitted_helps:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {metric_type}")
            _emitted_helps.add(name)
        if labels:
            label_str = ",".join(
                f'{k}="{str(v).replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"'
                for k, v in labels.items()
            )
            lines.append(f"{name}{{{label_str}}} {value}")
        else:
            lines.append(f"{name} {value}")

    # App info
    _emit(
        "ferment_app_info", "Info application (toujours 1)", "gauge", 1,
        {"env": os.environ.get("ENV", "development")},
    )

    # EasyBeer circuit breaker
    try:
        from common.easybeer._client import (
            circuit_breaker_state,
            is_rate_limited,
        )
        from common.easybeer._client import (
            is_configured as eb_configured,
        )
        cb = circuit_breaker_state()
        cb_remaining = max(0.0, float(cb.get("remaining", 0) or 0))
        _emit(
            "ferment_easybeer_configured",
            "1 si EasyBeer a des credentials configurés", "gauge",
            1 if eb_configured() else 0,
        )
        _emit(
            "ferment_easybeer_circuit_breaker_open",
            "1 si le circuit-breaker EasyBeer est ouvert", "gauge",
            1 if cb_remaining > 0 else 0,
        )
        _emit(
            "ferment_easybeer_circuit_breaker_failure_count",
            "Compteur d'échecs 5xx consécutifs (reset à 0 au succès)", "gauge",
            int(cb.get("failures", 0) or 0),
        )
        _emit(
            "ferment_easybeer_rate_limit_remaining_seconds",
            "Secondes restantes avant la fin du rate-limit EasyBeer (0 si inactif)", "gauge",
            float(is_rate_limited() or 0),
        )
    except Exception:
        _log.debug("Échec collecte metrics EasyBeer", exc_info=True)

    # Cache DB L2
    try:
        from db.conn import run_sql as _run_sql
        rows = _run_sql("SELECT COUNT(*) AS n FROM eb_cache") or []
        _emit(
            "ferment_eb_cache_entries",
            "Nombre d'entrées dans le cache DB L2 EasyBeer", "gauge",
            int(rows[0]["n"]) if rows else 0,
        )
    except Exception:
        _log.debug("Échec collecte metrics cache", exc_info=True)

    # Sync rate limiter
    try:
        from common.sync.rate_limit import state_snapshot
        snap = state_snapshot()
        _emit(
            "ferment_sync_rate_limit_active_keys",
            "Nombre de clés API actives dans la fenêtre rate-limit courante",
            "gauge", len(snap),
        )
        for key_id, hits in snap.items():
            # Tronque la clé à 8 car pour éviter de leaker un secret
            short = key_id[:8]
            _emit(
                "ferment_sync_rate_limit_hits",
                "Nombre de hits dans la fenêtre rate-limit courante (par clé)",
                "gauge", hits, {"key_id": short},
            )
    except Exception:
        _log.debug("Échec collecte metrics sync", exc_info=True)

    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4; charset=utf-8",
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
    """Vérifie la clé API sync. Retourne (auth_info, None) ou (None, error_response).

    Applique également un rate-limit per-key (60 req/min) pour protéger la DB
    contre un agent mal configuré (retry-storm). Les requêtes au-delà sont
    rejetées avec HTTP 429 + header Retry-After.
    """
    from common.sync.api_key import verify_api_key
    from common.sync.rate_limit import check as rl_check

    raw_key = _extract_bearer_key(request)
    if not raw_key:
        return None, JSONResponse({"error": "Missing Authorization header"}, status_code=401)
    auth_info = verify_api_key(raw_key)
    if not auth_info:
        return None, JSONResponse({"error": "Invalid API key"}, status_code=401)

    # Rate-limit par clé (pas par IP : plusieurs agents derrière un NAT
    # partageraient l'IP mais ont chacun leur clé).
    key_id = str(auth_info.get("key_id") or raw_key)
    allowed, retry_after = rl_check(key_id)
    if not allowed:
        _sync_log.warning(
            "Rate-limit dépassé pour key %s (retry in %ds)", key_id[:8], retry_after,
        )
        return None, JSONResponse(
            {"error": "Rate limit exceeded", "retry_after": retry_after},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    return auth_info, None


@app.get("/api/sync/pending")
async def _sync_pending(request: Request):
    """Agent Windows : récupère la dernière opération pending."""
    auth_info, err = _verify_sync_auth(request)
    if err:
        return err

    import json

    from db.conn import run_sql

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

    from common.sync import create_sync_operation
    from common.sync.collector import collect_label_data

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


# ─── Décodage de code-barres (page Étiquettes palette) ──────────────────────

_MAX_BARCODE_IMAGE_BYTES = 12 * 1024 * 1024  # 12 MB (photo iPhone HEIC/JPG)

# Rate limit : ~30 scans / minute / utilisateur. Largement au-dessus de
# l'usage normal en entrepôt (un opérateur scan max 1-2 cartons/min en
# pratique), mais bloque les boucles JS accidentelles ou un script qui
# saturerait le pool de threads (decode zxing-cpp + PIL en thread).
_SCAN_RATE_LIMIT_WINDOW_SECONDS = 60
_SCAN_RATE_LIMIT_MAX = 30
# Fenêtre glissante des timestamps de scan par utilisateur. Mémoire bornée :
# O(_SCAN_RATE_LIMIT_MAX × nb users actifs) → négligeable (~10 KB pour 100 users).
_scan_rate_limit_state: dict[str, deque] = {}


def _scan_rate_limit_check(user_key: str) -> bool:
    """Retourne True si la requête est autorisée, False si rate-limitée."""
    now = _time.monotonic()
    times = _scan_rate_limit_state.setdefault(user_key, deque())
    cutoff = now - _SCAN_RATE_LIMIT_WINDOW_SECONDS
    while times and times[0] < cutoff:
        times.popleft()
    if len(times) >= _SCAN_RATE_LIMIT_MAX:
        return False
    times.append(now)
    return True


@app.post("/api/scan-barcode")
async def _api_scan_barcode(request: Request):
    """Décode un code-barres depuis une image uploadée (caméra iPhone/iPad).

    Auth : session NiceGUI + tenant_id obligatoire.
    Body : multipart/form-data avec un champ 'file' (image JPG/PNG/HEIC).
    Retour : ``{"ean": "<digits>"}`` ou ``{"error": "<msg>"}`` (HTTP 4xx).
    """
    user_store = app.storage.user
    if not user_store.get("authenticated"):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    # Tenant check explicite : la matrice codes-barres EasyBeer est
    # globale à la brasserie configurée (EASYBEER_ID_BRASSERIE), donc
    # aujourd'hui mono-tenant. Mais on durcit dès maintenant pour le
    # jour où NIKO devient un tenant séparé : sans tenant_id en session,
    # on refuse l'accès.
    tenant_id = user_store.get("tenant_id")
    if not tenant_id:
        return JSONResponse({"error": "No tenant in session"}, status_code=403)

    # Rate limit par utilisateur (fallback sur tenant_id si pas d'email)
    user_key = str(user_store.get("email") or tenant_id)
    if not _scan_rate_limit_check(user_key):
        _log.warning("Scan barcode : rate-limit atteint pour %s", user_key)
        return JSONResponse(
            {
                "error": (
                    f"Trop de scans (limite {_SCAN_RATE_LIMIT_MAX}/minute). "
                    "Attends une minute."
                ),
            },
            status_code=429,
        )

    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "Invalid form data"}, status_code=400)

    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "Missing 'file' field"}, status_code=400)

    # Validation MIME : on n'accepte que les images (le navigateur capture file
    # envoie du image/jpeg, image/png ou rarement image/heic).
    content_type = (getattr(upload, "content_type", "") or "").lower()
    if content_type and not content_type.startswith("image/"):
        return JSONResponse(
            {"error": f"Type de fichier non supporté ({content_type}). Image attendue."},
            status_code=415,
        )

    try:
        image_bytes = await upload.read()
    except Exception:
        _log.exception("Erreur lecture upload scan-barcode")
        return JSONResponse({"error": "Cannot read uploaded file"}, status_code=400)

    if len(image_bytes) == 0:
        return JSONResponse({"error": "Empty file"}, status_code=400)
    if len(image_bytes) > _MAX_BARCODE_IMAGE_BYTES:
        return JSONResponse(
            {"error": f"File too large ({len(image_bytes) // 1024} KB > {_MAX_BARCODE_IMAGE_BYTES // 1024} KB)"},
            status_code=413,
        )

    from common.services.etiquette_palette_service import (
        extract_gs1_data_from_image,
        lookup_product_by_ean,
    )

    scan = await asyncio.to_thread(extract_gs1_data_from_image, image_bytes)
    if not scan:
        _log.info("Scan barcode : aucun code-barres détecté (%d KB)", len(image_bytes) // 1024)
        return JSONResponse({"error": "No barcode detected"}, status_code=200)

    ean = str(scan.get("ean") or "")
    product = await asyncio.to_thread(lookup_product_by_ean, ean)
    _log.info(
        "Scan barcode : ean=%s lot=%s product=%s",
        ean, scan.get("lot") or "—",
        (product or {}).get("designation") or "(non trouvé EB)",
    )

    ddm = scan.get("ddm")
    return JSONResponse({
        "ean": ean,
        "lot": scan.get("lot") or "",
        "ddm": ddm.isoformat() if ddm else None,
        "product": product,
    })


# ─── Scan SSCC palette (chargement camion) ─────────────────────────────────

@app.post("/api/scan-sscc")
async def _api_scan_sscc(request: Request):
    """Décode une image (caméra iPhone/iPad), extrait l'AI 00 (SSCC) et
    vérifie l'état de la palette (libre, déjà chargée, inconnue…).

    Auth : session NiceGUI + tenant_id obligatoire.
    Body : multipart/form-data avec un champ ``file`` (image JPG/PNG/HEIC).
    Retour JSON :
      ``{"status": "ok|unknown|already_loaded|inconsistent",
         "palette": {...} | null,
         "existing_ramasse_id": "..." | null,
         "error": "..." | ""}``
    """
    user_store = app.storage.user
    if not user_store.get("authenticated"):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    tenant_id = user_store.get("tenant_id")
    if not tenant_id:
        return JSONResponse({"error": "No tenant"}, status_code=403)

    # Rate limit partagé avec /api/scan-barcode (même opérateur peut faire
    # les deux mais ce sont des actions distinctes — clé séparée).
    user_key = f"sscc:{user_store.get('email') or tenant_id}"
    if not _scan_rate_limit_check(user_key):
        return JSONResponse(
            {"error": f"Trop de scans (limite {_SCAN_RATE_LIMIT_MAX}/min)"},
            status_code=429,
        )

    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "Invalid form data"}, status_code=400)

    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "Missing 'file' field"}, status_code=400)

    content_type = (getattr(upload, "content_type", "") or "").lower()
    if content_type and not content_type.startswith("image/"):
        return JSONResponse(
            {"error": f"Type de fichier non supporté ({content_type})"},
            status_code=415,
        )

    try:
        image_bytes = await upload.read()
    except Exception:
        _log.exception("Erreur lecture upload scan-sscc")
        return JSONResponse({"error": "Cannot read uploaded file"}, status_code=400)

    if not image_bytes:
        return JSONResponse({"error": "Empty file"}, status_code=400)
    if len(image_bytes) > _MAX_BARCODE_IMAGE_BYTES:
        return JSONResponse(
            {"error": f"File too large ({len(image_bytes) // 1024} KB)"},
            status_code=413,
        )

    from common.services.loading_service import lookup_sscc_from_image
    result = await asyncio.to_thread(
        lookup_sscc_from_image, image_bytes, str(tenant_id),
    )

    # Sérialisation du résultat
    palette_dict = None
    if result.palette:
        p = result.palette
        palette_dict = {
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
    _log.info(
        "Scan SSCC : status=%s sscc=%s", result.status,
        (palette_dict or {}).get("sscc", "?"),
    )
    return JSONResponse({
        "status": result.status,
        "palette": palette_dict,
        "existing_ramasse_id": result.existing_ramasse_id,
        "error": result.error_message,
    })


@app.post("/api/lookup-sscc")
async def _api_lookup_sscc(request: Request):
    """Version "saisie manuelle" du scan SSCC : prend un SSCC en JSON
    (déjà tapé par l'opérateur) et retourne le même format de réponse
    que /api/scan-sscc. Pas de décodage image.
    """
    user_store = app.storage.user
    if not user_store.get("authenticated"):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    tenant_id = user_store.get("tenant_id")
    if not tenant_id:
        return JSONResponse({"error": "No tenant"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        body = {}
    sscc_raw = str((body or {}).get("sscc") or "").strip()
    if not sscc_raw:
        return JSONResponse({"error": "Missing 'sscc' field"}, status_code=400)

    from common.services.loading_service import lookup_sscc
    result = await asyncio.to_thread(lookup_sscc, sscc_raw, str(tenant_id))

    palette_dict = None
    if result.palette:
        p = result.palette
        palette_dict = {
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
    return JSONResponse({
        "status": result.status,
        "palette": palette_dict,
        "existing_ramasse_id": result.existing_ramasse_id,
        "error": result.error_message,
    })


# ─── Print jobs queue (Brother QL via agent local) ──────────────────────────

# Token bearer partagé entre le VPS et l'agent Windows. À générer une fois
# (ex: openssl rand -hex 32) et coller dans le .env du VPS et de l'agent.
# Si non défini, l'API d'agent répond 503 (pas de queue active).
_PRINT_AGENT_TOKEN = os.environ.get("PRINT_AGENT_TOKEN", "").strip()
# Tenant unique côté agent : pour l'instant mono-tenant. ID du tenant
# Symbiose Kéfir résolu au démarrage. Si on passe multi-tenant, un token
# par agent + table de mapping fera le travail.
_PRINT_AGENT_TENANT_ID = os.environ.get("PRINT_AGENT_TENANT_ID", "").strip()

# File asyncio par tenant : push depuis create_print_job → wake-up des
# long-polls en attente. maxsize=1 : on ne stocke qu'un signal pending,
# c'est suffisant (l'agent re-vérifie la DB à chaque réveil).
_print_pending_signals: dict[str, asyncio.Queue] = {}


def _print_signal_queue(tenant_id: str) -> asyncio.Queue:
    """Retourne (ou crée) la queue de signaux pour un tenant."""
    q = _print_pending_signals.get(tenant_id)
    if q is None:
        q = asyncio.Queue(maxsize=1)
        _print_pending_signals[tenant_id] = q
    return q


def _signal_new_print_job(tenant_id: str) -> None:
    """Réveille les long-polls en attente. Coalesce les signaux."""
    q = _print_signal_queue(tenant_id)
    try:
        q.put_nowait("new")
    except asyncio.QueueFull:
        # Un signal est déjà en attente — pas la peine d'en empiler un autre.
        pass


def _check_agent_auth(request: Request) -> str | None:
    """Vérifie le bearer token agent. Retourne tenant_id si OK, None sinon."""
    if not _PRINT_AGENT_TOKEN or not _PRINT_AGENT_TENANT_ID:
        return None
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    # Comparaison constant-time pour éviter un timing attack.
    import hmac
    if not hmac.compare_digest(token, _PRINT_AGENT_TOKEN):
        return None
    return _PRINT_AGENT_TENANT_ID


@app.post("/api/print-jobs")
async def _api_create_print_job(request: Request):
    """Crée un job d'impression depuis la session opérateur (iPhone/iPad).

    Auth : session NiceGUI + tenant_id. Body : multipart/form-data avec
    'file' (PDF), 'filename' (str), 'n_copies' (int, optionnel).
    """
    user_store = app.storage.user
    if not user_store.get("authenticated"):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    tenant_id = user_store.get("tenant_id")
    if not tenant_id:
        return JSONResponse({"error": "No tenant"}, status_code=403)

    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "Invalid form data"}, status_code=400)

    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "Missing 'file' field"}, status_code=400)

    content_type = (getattr(upload, "content_type", "") or "").lower()
    if content_type and not content_type.startswith("application/pdf"):
        return JSONResponse(
            {"error": f"Type de fichier non supporté ({content_type}). PDF attendu."},
            status_code=415,
        )

    try:
        pdf_bytes = await upload.read()
    except Exception:
        _log.exception("Erreur lecture upload print-job")
        return JSONResponse({"error": "Cannot read uploaded file"}, status_code=400)

    # Borne de taille : un PDF étiquette palette fait ~50 KB. 5 MB de marge.
    if not pdf_bytes or len(pdf_bytes) > 5 * 1024 * 1024:
        return JSONResponse(
            {"error": f"PDF size out of bounds ({len(pdf_bytes)} bytes)"},
            status_code=413,
        )

    filename = str(form.get("filename") or "etiquette.pdf")
    try:
        n_copies = max(1, min(10, int(form.get("n_copies") or 1)))
    except (TypeError, ValueError):
        n_copies = 1

    from common.services.print_jobs_service import create_print_job
    job_id = await asyncio.to_thread(
        create_print_job,
        str(tenant_id),
        user_email=str(user_store.get("email") or ""),
        pdf_bytes=pdf_bytes,
        filename=filename,
        n_copies=n_copies,
    )
    if not job_id:
        return JSONResponse({"error": "DB insert failed"}, status_code=500)

    # Réveille les long-polls en attente côté agent.
    _signal_new_print_job(str(tenant_id))
    _log.info("Print job %d créé pour tenant %s (%d KB)", job_id, tenant_id, len(pdf_bytes) // 1024)
    return JSONResponse({"id": job_id, "status": "pending"}, status_code=201)


@app.get("/api/print-jobs/next")
async def _api_next_print_job(request: Request):
    """Long-polling : l'agent attend ici jusqu'à ce qu'un job soit dispo.

    Auth : bearer token PRINT_AGENT_TOKEN.
    Réponse :
      - 200 + JSON {id, filename, n_copies, pdf_b64} si un job est dispo
      - 204 No Content si aucun job pendant 25 sec (l'agent reconnecte)
      - 401 si auth invalide
      - 503 si la queue n'est pas configurée (env var manquante)
    """
    tenant_id = _check_agent_auth(request)
    if tenant_id is None:
        if not _PRINT_AGENT_TOKEN:
            return JSONResponse({"error": "Print agent not configured"}, status_code=503)
        return JSONResponse({"error": "Invalid token"}, status_code=401)

    from common.services.print_jobs_service import (
        reset_stuck_jobs,
        take_next_pending_job,
    )

    # Watchdog opportuniste : remet en pending les jobs bloqués > 5 min en
    # 'printing' (cas où l'agent crash mid-print). Coût négligeable.
    await asyncio.to_thread(reset_stuck_jobs, tenant_id, 5)

    # Premier check : si un job traîne déjà en pending, on le prend de suite
    job = await asyncio.to_thread(take_next_pending_job, tenant_id)
    if job is None:
        # Long-poll : on attend jusqu'à 25 sec un signal de nouvelle job.
        # 25 sec est sous le timeout par défaut Caddy (60s) et requests (30s)
        # côté agent, donc on retourne avant que la connexion expire.
        try:
            await asyncio.wait_for(_print_signal_queue(tenant_id).get(), timeout=25.0)
        except TimeoutError:
            return Response(status_code=204)
        # Réveillé : on tente de prendre un job
        job = await asyncio.to_thread(take_next_pending_job, tenant_id)
        if job is None:
            return Response(status_code=204)

    import base64
    return JSONResponse({
        "id": job.id,
        "filename": job.filename,
        "n_copies": job.n_copies,
        "pdf_b64": base64.b64encode(job.pdf_bytes).decode("ascii"),
        "created_at": job.created_at.isoformat() if job.created_at else None,
    })


@app.post("/api/print-jobs/{job_id}/done")
async def _api_print_job_done(job_id: int, request: Request):
    """L'agent confirme l'impression réussie."""
    tenant_id = _check_agent_auth(request)
    if tenant_id is None:
        return JSONResponse({"error": "Invalid token"}, status_code=401)
    from common.services.print_jobs_service import mark_job_printed
    ok = await asyncio.to_thread(mark_job_printed, tenant_id, int(job_id))
    if not ok:
        return JSONResponse({"error": "Job not found or wrong status"}, status_code=404)
    _log.info("Print job %d imprimé (tenant %s)", job_id, tenant_id)
    return JSONResponse({"status": "printed"})


@app.post("/api/print-jobs/{job_id}/error")
async def _api_print_job_error(job_id: int, request: Request):
    """L'agent signale une erreur d'impression. Body : JSON {error: str}."""
    tenant_id = _check_agent_auth(request)
    if tenant_id is None:
        return JSONResponse({"error": "Invalid token"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    error_msg = str((body or {}).get("error") or "unknown")[:500]
    from common.services.print_jobs_service import mark_job_error
    ok = await asyncio.to_thread(mark_job_error, tenant_id, int(job_id), error_msg)
    if not ok:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    _log.warning("Print job %d en erreur (tenant %s) : %s", job_id, tenant_id, error_msg)
    return JSONResponse({"status": "error"})


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
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL)
        _do_cleanup()


async def _daily_client_sync_loop() -> None:
    """Boucle infinie : sync des clients EasyBeer tous les jours à 3h du matin (Paris)."""
    import datetime as _dt

    from dateutil.tz import gettz

    _PARIS = gettz("Europe/Paris")
    _SYNC_HOUR = 3
    _SYNC_MINUTE = 0

    while True:
        try:
            # Calculer le prochain 3h00 Paris
            now = _dt.datetime.now(_PARIS)
            target = now.replace(hour=_SYNC_HOUR, minute=_SYNC_MINUTE, second=0, microsecond=0)
            if target <= now:
                target += _dt.timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            _log.info("Client sync: prochain run à %s (dans %.0fh)", target.strftime("%H:%M"), wait_seconds / 3600)
            await asyncio.sleep(wait_seconds)

            # Résoudre le tenant
            tenant_name = os.environ.get("ALLOWED_TENANTS", "").split(",")[0].strip()
            if not tenant_name:
                _log.debug("Client sync: pas de ALLOWED_TENANTS, skip")
                continue

            from common.auth import ensure_tenant_id
            from common.client_cache import sync_clients
            from common.easybeer import is_configured as _eb_ok

            if not _eb_ok():
                _log.debug("Client sync: EasyBeer non configuré, skip")
                continue

            tid = ensure_tenant_id(tenant_name)
            _log.info("Client sync: démarrage pour tenant '%s'", tenant_name)
            result = await asyncio.to_thread(sync_clients, tid)
            _log.info("Client sync terminé: %s", result)

        except Exception:
            _log.exception("Erreur dans le sync nocturne clients")
            # Attendre 1h avant de réessayer en cas d'erreur
            await asyncio.sleep(3600)


@app.on_startup
async def _startup_cleanup():
    """Vérifications de sécurité + nettoyage initial + démarrage du timer périodique."""

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

    # Démarrer le sync nocturne des clients EasyBeer (3h du matin Paris)
    asyncio.ensure_future(_daily_client_sync_loop())

    # Démarrer la boucle de cache EasyBeer (sync toutes les 60s)
    from common.eb_sync_loop import eb_cache_sync_loop
    asyncio.ensure_future(eb_cache_sync_loop())


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
    """Exige un vrai secret pour signer les cookies de session (>= 32 chars)."""
    secret = os.environ.get("NICEGUI_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "NICEGUI_SECRET manquant — génère-en un :\n"
            '  python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    if len(secret) < 32:
        raise RuntimeError(
            f"NICEGUI_SECRET trop court ({len(secret)} chars, 32 min). "
            'Régénère : python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
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
