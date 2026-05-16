"""
tests/test_mobile_v1_endpoints.py
==================================
Tests des endpoints ``common/mobile_v1.py`` via Starlette TestClient.

On monte une mini-app Starlette via ``register_routes(app)`` au lieu de
booter toute la stack NiceGUI. On mock à coup de ``patch`` les fonctions
DB (verify_mobile_token, lookup_product_by_ean, services, etc.) pour
tester le **glue code de transport** : parsing body, auth, format JSON,
status codes, isolation tenant.

Couvre les cas critiques :
  - 401 si pas de token (toutes les routes Bearer)
  - 401 si token invalide
  - 403 si rôle insuffisant (sscc-log = admin)
  - Body parsing OK / KO (400 si invalide)
  - Isolation tenant : un token tenant A ne peut pas archiver/réimprimer
    un label tenant B (le service retourne False/None → endpoint 404)
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from common.mobile_v1 import register_routes

# ─── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    """Mini FastAPI app avec uniquement les routes mobile v1 enregistrées.

    On utilise FastAPI (et pas Starlette nu) parce que ``register_routes``
    utilise ``app.post(...)`` / ``app.get(...)`` qui sont des méthodes FastAPI.
    En prod, c'est l'app NiceGUI (qui est FastAPI sous le capot) qui les fournit.
    """
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer fake-token"}


def _user(role: str = "user", tenant: str = "tenant-A"):
    return {
        "id": "user-1",
        "tenant_id": tenant,
        "email": "test@symbiose.fr",
        "role": role,
    }


# ─── 401 / Auth tests ──────────────────────────────────────────────────────

class TestUnauthorized:
    """Toutes les routes Bearer doivent retourner 401 si pas de token."""

    @pytest.mark.parametrize("method,path", [
        ("post", "/api/v1/decode-gs1"),
        ("post", "/api/v1/print-palette"),
        ("post", "/api/v1/labels/1/archive"),
        ("post", "/api/v1/labels/1/reprint"),
        ("get", "/api/v1/today-labels"),
        ("get", "/api/v1/home-summary"),
        ("get", "/api/v1/sscc-log"),
    ])
    def test_no_token_returns_401(self, client, method, path):
        resp = client.request(method.upper(), path)
        assert resp.status_code == 401
        assert resp.json() == {"error": "Invalid or expired token"}

    @patch("common.mobile_v1.verify_mobile_token")
    def test_invalid_token_returns_401(self, mock_verify, client):
        mock_verify.return_value = None
        resp = client.get(
            "/api/v1/today-labels",
            headers={"Authorization": "Bearer bad"},
        )
        assert resp.status_code == 401


class TestLogin:
    """POST /api/v1/auth/login — ne nécessite pas de token au préalable."""

    def test_missing_body_returns_400(self, client):
        resp = client.post(
            "/api/v1/auth/login",
            headers={"Content-Type": "application/json"},
            content=b"not json",
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_missing_email_or_password_returns_400(self, client):
        resp = client.post("/api/v1/auth/login", json={"email": ""})
        assert resp.status_code == 400

    @patch("common.mobile_v1.create_mobile_token")
    @patch("common.auth.authenticate")
    def test_bad_credentials_returns_401(self, mock_auth, mock_create, client):
        mock_auth.return_value = None
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "wrong@test.fr", "password": "xxx"},
        )
        assert resp.status_code == 401
        assert resp.json() == {"error": "Invalid credentials"}
        # Pas de token créé en cas d'échec
        mock_create.assert_not_called()

    @patch("common.mobile_v1.create_mobile_token")
    @patch("common.auth.authenticate")
    def test_success_returns_token(self, mock_auth, mock_create, client):
        mock_auth.return_value = {
            "id": "user-1",
            "tenant_id": "tenant-A",
            "email": "ok@test.fr",
            "role": "admin",
        }
        mock_create.return_value = ("token-abc", _dt.datetime(2027, 1, 1, tzinfo=_dt.UTC))
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": "ok@test.fr", "password": "pw", "device_name": "iPhone"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"] == "token-abc"
        assert body["tenant_id"] == "tenant-A"
        assert body["user"]["email"] == "ok@test.fr"
        assert body["user"]["role"] == "admin"


# ─── decode-gs1 ────────────────────────────────────────────────────────────

class TestDecodeGs1:
    @patch("common.mobile_v1.verify_mobile_token")
    def test_missing_code_returns_400(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user()
        resp = client.post("/api/v1/decode-gs1", headers=auth_headers, json={})
        assert resp.status_code == 400

    @patch("common.mobile_v1.verify_mobile_token")
    def test_invalid_code_returns_400(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user()
        # Code garbage → parse_gs1_to_entry retourne None
        resp = client.post(
            "/api/v1/decode-gs1",
            headers=auth_headers,
            json={"code": "garbage"},
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    @patch("common.services.etiquette_palette_service.lookup_product_by_ean")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_success_with_product_found(
        self, mock_verify, mock_lookup, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_lookup.return_value = {
            "designation": "Kéfir Gingembre",
            "marque": "SYMBIOSE",
            "fmt": "12x33",
            "pcb": 12,
            "gout": "Gingembre",
            "ean_uvc": "",
        }
        resp = client.post(
            "/api/v1/decode-gs1",
            headers=auth_headers,
            json={"code": "(01)03770014427250(15)270511(10)110527"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ean"] == "03770014427250"
        assert body["lot"] == "110527"
        assert body["ddm"] == "2027-05-11"
        assert body["product"]["designation"] == "Kéfir Gingembre"
        # image_url et palette_layout sont enrichis par l'endpoint
        assert "image_url" in body["product"]
        assert "palette_layout" in body["product"]


# ─── archive / reprint — isolation tenant ──────────────────────────────────

class TestArchiveLabel:
    """L'archivage doit échouer (404) si le label n'appartient pas au tenant."""

    @patch("common.services.etiquette_palette_service.set_label_archived")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_label_not_found_returns_404(
        self, mock_verify, mock_set, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        # Service retourne False (label inexistant ou autre tenant)
        mock_set.return_value = False
        resp = client.post("/api/v1/labels/9999/archive", headers=auth_headers)
        assert resp.status_code == 404

    @patch("common.services.etiquette_palette_service.set_label_archived")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_archive_passes_tenant_id_to_service(
        self, mock_verify, mock_set, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_set.return_value = _dt.datetime(2026, 5, 16, 14, 30, tzinfo=_dt.UTC)
        client.post("/api/v1/labels/42/archive", headers=auth_headers)
        # CRITIQUE : le tenant_id passé au service doit être celui du token.
        # Sinon fuite cross-tenant possible.
        call_args = mock_set.call_args
        assert call_args[0][0] == "tenant-A"
        assert call_args[0][1] == 42

    @patch("common.services.etiquette_palette_service.set_label_archived")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_archive_body_forces_state(
        self, mock_verify, mock_set, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_set.return_value = None
        client.post(
            "/api/v1/labels/42/archive",
            headers=auth_headers,
            json={"archived": False},
        )
        # Le service reçoit archived=False
        assert mock_set.call_args[1]["archived"] is False


class TestReprintLabel:
    @patch("common.services.etiquette_palette_service.get_history_entry")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_label_not_found_returns_404(
        self, mock_verify, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_get.return_value = None  # autre tenant ou inexistant
        resp = client.post("/api/v1/labels/9999/reprint", headers=auth_headers)
        assert resp.status_code == 404

    @patch("common.services.etiquette_palette_service.get_history_entry")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_reprint_filters_by_tenant(
        self, mock_verify, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_get.return_value = None
        client.post("/api/v1/labels/42/reprint", headers=auth_headers)
        # get_history_entry doit être appelé avec le tenant_id du token
        assert mock_get.call_args[0][0] == "tenant-A"


# ─── home-summary / today-labels ───────────────────────────────────────────

class TestHomeSummary:
    @patch("common.services.etiquette_palette_service.list_recent_labels")
    @patch("common.services.etiquette_palette_service.count_today_and_month")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_returns_counts_and_recent(
        self, mock_verify, mock_counts, mock_recent, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_counts.return_value = {"today_count": 5, "month_count": 42}
        mock_recent.return_value = []  # liste vide OK pour ce test
        resp = client.get("/api/v1/home-summary", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["today_count"] == 5
        assert body["month_count"] == 42
        assert body["recent"] == []


class TestTodayLabels:
    @patch("common.services.etiquette_palette_service.list_today_labels")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_passes_tenant_to_service(
        self, mock_verify, mock_list, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-X")
        mock_list.return_value = [{"id": 1, "sscc": "..."}]
        resp = client.get("/api/v1/today-labels", headers=auth_headers)
        assert resp.status_code == 200
        assert mock_list.call_args[0][0] == "tenant-X"
        assert resp.json()["labels"][0]["id"] == 1


# ─── sscc-log : admin only ────────────────────────────────────────────────

class TestSsccLog:
    @patch("common.mobile_v1.verify_mobile_token")
    def test_non_admin_returns_403(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="user")
        resp = client.get("/api/v1/sscc-log", headers=auth_headers)
        assert resp.status_code == 403
        assert "Admin" in resp.json().get("error", "")

    @patch("common.mobile_v1.verify_mobile_token")
    def test_operateur_returns_403(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="operateur")
        resp = client.get("/api/v1/sscc-log", headers=auth_headers)
        assert resp.status_code == 403

    @patch("common.services.sscc_service.list_sscc_log")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_admin_can_access(
        self, mock_verify, mock_list, client, auth_headers
    ):
        mock_verify.return_value = _user(role="admin")
        mock_list.return_value = []
        resp = client.get("/api/v1/sscc-log", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"entries": []}

    @patch("common.services.sscc_service.list_sscc_log")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_admin_passes_filters_to_service(
        self, mock_verify, mock_list, client, auth_headers
    ):
        mock_verify.return_value = _user(role="admin", tenant="tenant-X")
        mock_list.return_value = []
        client.get(
            "/api/v1/sscc-log?limit=100&date_from=2026-01-01&date_to=2026-12-31&lot=ABC",
            headers=auth_headers,
        )
        assert mock_list.call_args[0][0] == "tenant-X"
        kwargs = mock_list.call_args[1]
        assert kwargs["limit"] == 100
        assert kwargs["date_from"] == _dt.date(2026, 1, 1)
        assert kwargs["date_to"] == _dt.date(2026, 12, 31)
        assert kwargs["lot_filter"] == "ABC"


# ─── preview-palette (sans SSCC ni history) ───────────────────────────────

class TestPreviewPalette:
    @patch("common.mobile_v1.verify_mobile_token")
    def test_missing_fields_returns_400(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user()
        resp = client.post(
            "/api/v1/preview-palette", headers=auth_headers, json={"ean": "x"}
        )
        assert resp.status_code == 400

    @patch("common.services.etiquette_palette_service.preview_palette_label")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_product_not_found_returns_404(
        self, mock_verify, mock_preview, client, auth_headers
    ):
        from common.services.etiquette_palette_service import ProductNotFoundError

        mock_verify.return_value = _user()
        mock_preview.side_effect = ProductNotFoundError("Produit introuvable")
        resp = client.post(
            "/api/v1/preview-palette",
            headers=auth_headers,
            json={
                "ean": "999", "lot": "L1", "ddm": "2027-05-11",
                "case_count": 1, "full_pallet": True, "n_copies": 1,
            },
        )
        assert resp.status_code == 404

    @patch("common.services.etiquette_palette_service.preview_palette_label")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_success_returns_pdf_without_sscc_side_effects(
        self, mock_verify, mock_preview, client, auth_headers
    ):
        """Le preview NE DOIT PAS appeler generate_sscc ni save_label_history.
        On vérifie en mockant le service : il doit recevoir UNIQUEMENT les
        params de génération (pas de tenant_id pour audit).
        """
        mock_verify.return_value = _user()
        mock_preview.return_value = b"%PDF-preview"
        resp = client.post(
            "/api/v1/preview-palette",
            headers=auth_headers,
            json={
                "ean": "03770014427250", "lot": "110527", "ddm": "2027-05-11",
                "case_count": 96, "full_pallet": True, "n_copies": 1,
            },
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == b"%PDF-preview"
        # preview_palette_label n'a pas de paramètre tenant_id (pas d'audit).
        # On vérifie qu'il a été appelé avec les bons champs et qu'il n'y a
        # PAS de user_email (différence clé avec generate_and_save_palette_label).
        call_kwargs = mock_preview.call_args.kwargs
        assert call_kwargs["ean"] == "03770014427250"
        assert call_kwargs["case_count"] == 96
        assert "user_email" not in call_kwargs
        assert "tenant_id" not in call_kwargs


# ─── print-palette ─────────────────────────────────────────────────────────

class TestPrintPalette:
    @patch("common.mobile_v1.verify_mobile_token")
    def test_missing_fields_returns_400(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user()
        resp = client.post(
            "/api/v1/print-palette", headers=auth_headers, json={"ean": "x"}
        )
        assert resp.status_code == 400

    @patch("common.mobile_v1.verify_mobile_token")
    def test_zero_case_count_returns_400(
        self, mock_verify, client, auth_headers
    ):
        mock_verify.return_value = _user()
        resp = client.post(
            "/api/v1/print-palette",
            headers=auth_headers,
            json={
                "ean": "03770014427250",
                "lot": "110527",
                "ddm": "2027-05-11",
                "case_count": 0,
                "full_pallet": True,
                "n_copies": 1,
            },
        )
        assert resp.status_code == 400

    @patch("common.mobile_v1.verify_mobile_token")
    def test_invalid_ddm_returns_400(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user()
        resp = client.post(
            "/api/v1/print-palette",
            headers=auth_headers,
            json={
                "ean": "03770014427250",
                "lot": "110527",
                "ddm": "nope",
                "case_count": 12,
                "full_pallet": True,
                "n_copies": 1,
            },
        )
        assert resp.status_code == 400

    @patch("common.services.etiquette_palette_service.generate_and_save_palette_label")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_product_not_found_returns_404(
        self, mock_verify, mock_gen, client, auth_headers
    ):
        from common.services.etiquette_palette_service import ProductNotFoundError

        mock_verify.return_value = _user()
        mock_gen.side_effect = ProductNotFoundError("Produit introuvable pour EAN x")
        resp = client.post(
            "/api/v1/print-palette",
            headers=auth_headers,
            json={
                "ean": "999",
                "lot": "L1",
                "ddm": "2027-05-11",
                "case_count": 1,
                "full_pallet": True,
                "n_copies": 1,
            },
        )
        assert resp.status_code == 404

    @patch("common.services.etiquette_palette_service.generate_and_save_palette_label")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_success_returns_pdf_bytes(
        self, mock_verify, mock_gen, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_gen.return_value = (b"%PDF-fake", "001234567890123456", 42)
        resp = client.post(
            "/api/v1/print-palette",
            headers=auth_headers,
            json={
                "ean": "03770014427250",
                "lot": "110527",
                "ddm": "2027-05-11",
                "case_count": 96,
                "full_pallet": True,
                "n_copies": 1,
            },
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == b"%PDF-fake"
        # Service appelé avec le tenant du token (pas un autre)
        assert mock_gen.call_args[0][0] == "tenant-A"
