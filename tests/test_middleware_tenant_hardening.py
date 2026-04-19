"""Tests d'intégration du AuthMiddleware — hardening tenant_id par requête.

Vérifie les garanties sécurité ajoutées par le middleware :
- Session authentifiée sans tenant_id → logout forcé + redirect /login.
- Session avec tenant_id valide → passage + attribut request.state.tenant_id set.
- Chemins publics non protégés par la vérif tenant_id.

On instancie le middleware directement avec un mock d'``app.storage.user``
(un dict) pour éviter le setup complet NiceGUI.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _time_future() -> float:
    """Timestamp futur (+1h) pour court-circuiter la revalidation DB (> 5 min)."""
    return time.time() + 3600
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app_nicegui import AuthMiddleware


class _FakeStore(dict):
    """Stand-in pour app.storage.user."""


@pytest.fixture
def fake_store():
    """Dict partagé qui remplace app.storage.user pour le test."""
    store = _FakeStore()
    with patch("app_nicegui.app") as mock_app:
        mock_app.storage = SimpleNamespace(user=store)
        yield store


def _build_client(store):
    """Construit un TestClient minimal avec une route /ping protégée."""
    async def _ping(request: Request):
        # Vérifier que request.state.tenant_id a été posé par le middleware
        return JSONResponse({
            "ok": True,
            "tenant_id": getattr(request.state, "tenant_id", None),
        })

    async def _public(request: Request):
        return PlainTextResponse("public")

    async def _login(request: Request):
        return PlainTextResponse("login-page")

    app = Starlette(routes=[
        Route("/ping", _ping),
        Route("/login", _login),
        Route("/health", _public),
    ])
    app.add_middleware(AuthMiddleware)
    return TestClient(app, follow_redirects=False)


class TestTenantIdHardening:

    def test_authenticated_without_tenant_id_forces_logout(self, fake_store):
        """Session authentifiée mais tenant_id vide → redirect /login."""
        fake_store.update({
            "authenticated": True,
            "email": "u@test.fr",
            "tenant_id": "",  # VIDE — doit déclencher le logout
            "_server_validated_at": _time_future(),  # skip validation DB
        })
        client = _build_client(fake_store)
        r = client.get("/ping")
        assert r.status_code == 307
        assert r.headers["location"] == "/login"
        # Session purgée
        assert fake_store == {}

    def test_authenticated_with_none_tenant_id_forces_logout(self, fake_store):
        fake_store.update({
            "authenticated": True,
            "email": "u@test.fr",
            "tenant_id": None,
            "_server_validated_at": _time_future(),
        })
        client = _build_client(fake_store)
        r = client.get("/ping")
        assert r.status_code == 307
        assert fake_store == {}

    def test_authenticated_with_whitespace_tenant_id_forces_logout(self, fake_store):
        """Un tenant_id = '   ' ne doit pas passer (strip check)."""
        fake_store.update({
            "authenticated": True,
            "email": "u@test.fr",
            "tenant_id": "   ",
            "_server_validated_at": _time_future(),
        })
        client = _build_client(fake_store)
        r = client.get("/ping")
        assert r.status_code == 307

    def test_authenticated_with_valid_tenant_id_passes(self, fake_store):
        fake_store.update({
            "authenticated": True,
            "email": "u@test.fr",
            "tenant_id": "tenant-abc",
            "_server_validated_at": _time_future(),
        })
        client = _build_client(fake_store)
        r = client.get("/ping")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        # request.state.tenant_id doit avoir été posé par le middleware
        assert body["tenant_id"] == "tenant-abc"

    def test_unauthenticated_redirects_to_login(self, fake_store):
        client = _build_client(fake_store)
        r = client.get("/ping")
        assert r.status_code == 307
        assert r.headers["location"] == "/login"


class TestPublicPaths:

    def test_health_bypasses_tenant_check(self, fake_store):
        """Les chemins publics (/health) ne subissent pas la vérif tenant_id."""
        client = _build_client(fake_store)  # fake_store vide = non auth
        r = client.get("/health")
        assert r.status_code == 200
        assert r.text == "public"

    def test_login_bypasses_tenant_check(self, fake_store):
        client = _build_client(fake_store)
        r = client.get("/login")
        assert r.status_code == 200
        assert r.text == "login-page"
