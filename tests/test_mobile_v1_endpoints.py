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
        ("get", "/api/v1/cold-room-palettes"),
        ("get", "/api/v1/last-packaging"),
        ("get", "/api/v1/active-ramasses"),
        ("post", "/api/v1/loadings/previsionnel"),
        ("get", "/api/v1/loadings/abc-123"),
        ("post", "/api/v1/loadings/abc-123/scan"),
        ("post", "/api/v1/loadings/abc-123/finalize"),
        ("delete", "/api/v1/loadings/abc-123/palettes/123456789012345678"),
        ("get", "/api/v1/ramasses"),
        ("get", "/api/v1/ramasses/abc-123/pdf"),
        ("post", "/api/v1/ramasses/abc-123/mark-driver-passed"),
        ("post", "/api/v1/admin/production-sheets"),
        ("get", "/api/v1/admin/production-sheets"),
        ("get", "/api/v1/admin/brassins-en-cours"),
        ("get", "/api/v1/admin/conditionnement-by-lot"),
        ("get", "/api/v1/admin/production-sheets/abc-123"),
        ("patch", "/api/v1/admin/production-sheets/abc-123"),
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


# ─── Chargement camion (ramasse) ───────────────────────────────────────────

def _fake_palette(sscc: str = "123456789012345678", **kwargs):
    """Construit un ``PaletteInfo`` minimal pour les tests loading."""
    from common.services.loading_service import PaletteInfo

    defaults = dict(
        sscc=sscc,
        gtin_palette="03770014427250",
        lot="L110527",
        ddm=_dt.date(2027, 5, 11),
        case_count=96,
        designation="Kéfir Pêche",
        fmt="12x33",
        marque="SYMBIOSE",
        gout="Pêche",
        pcb=12,
        gtin_uvc="03770014427267",
        generated_at=_dt.datetime(2026, 5, 16, 14, 30, tzinfo=_dt.UTC),
    )
    defaults.update(kwargs)
    return PaletteInfo(**defaults)


class TestColdRoomPalettes:
    @patch("common.services.loading_service.list_palettes_in_cold_room")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_returns_palettes_list(
        self, mock_verify, mock_list, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_list.return_value = [
            _fake_palette(sscc="111111111111111111"),
            _fake_palette(sscc="222222222222222222", designation="Kombucha Citron"),
        ]
        resp = client.get("/api/v1/cold-room-palettes", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["palettes"]) == 2
        assert body["palettes"][0]["sscc"] == "111111111111111111"
        assert body["palettes"][1]["designation"] == "Kombucha Citron"
        # tenant scoping
        assert mock_list.call_args[0][0] == "tenant-A"

    @patch("common.services.loading_service.list_palettes_in_cold_room")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_empty_returns_empty_list(
        self, mock_verify, mock_list, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_list.return_value = []
        resp = client.get("/api/v1/cold-room-palettes", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"palettes": []}


class TestLastPackaging:
    @patch("common.ramasse_history.get_last_packaging_for_dest")
    @patch("common.ramasse.load_packaging_items")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_returns_items_and_last_for_default_sofripa(
        self, mock_verify, mock_items, mock_last, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_items.return_value = [
            {"id": "pal_bt_33", "label": "Palette Bouteilles 33cl",
             "unit": "palette", "active": True},
            {"id": "pal_bt_75", "label": "Palette Bouteilles 75cl",
             "unit": "palette", "active": True},
        ]
        mock_last.return_value = [
            {"label": "Palette Bouteilles 33cl", "qty": 2, "unit": "palette"},
            {"label": "Palette Bouteilles 75cl", "qty": 2, "unit": "palette"},
        ]
        resp = client.get("/api/v1/last-packaging", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["destinataire"] == "SOFRIPA"
        assert len(body["items"]) == 2
        assert body["items"][0]["label"] == "Palette Bouteilles 33cl"
        assert body["last_quantities"][0]["qty"] == 2
        # Default destinataire est SOFRIPA, et tenant_id propagé à get_last
        assert mock_items.call_args[0][0] == "SOFRIPA"
        assert mock_last.call_args[0] == ("SOFRIPA", "tenant-A")

    @patch("common.ramasse_history.get_last_packaging_for_dest")
    @patch("common.ramasse.load_packaging_items")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_custom_destinataire_query_param(
        self, mock_verify, mock_items, mock_last, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_items.return_value = []
        mock_last.return_value = []
        resp = client.get(
            "/api/v1/last-packaging?destinataire=AUTRE", headers=auth_headers,
        )
        assert resp.status_code == 200
        assert mock_items.call_args[0][0] == "AUTRE"


class TestActiveRamasses:
    @patch("common.ramasse_history.get_active_ramasse_for_dest")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_no_active_returns_empty_list(
        self, mock_verify, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_get.return_value = None
        resp = client.get("/api/v1/active-ramasses", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"ramasses": []}

    @patch("common.ramasse_history.get_active_ramasse_for_dest")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_returns_active_ramasse_with_meta(
        self, mock_verify, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_get.return_value = {
            "id": "ramasse-abc",
            "date_ramasse": _dt.date(2026, 5, 20),
            "destinataire": "SOFRIPA",
            "status": "previsionnel",
            "total_palettes": 5,
            "total_cartons": 480,
            "total_poids_kg": 4000,
            "version": 1,
            "created_by_email": "ops@symbiose.fr",
            "created_at": _dt.datetime(2026, 5, 19, 18, 30, tzinfo=_dt.UTC),
        }
        resp = client.get("/api/v1/active-ramasses", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["ramasses"]) == 1
        r = body["ramasses"][0]
        assert r["id"] == "ramasse-abc"
        assert r["status"] == "previsionnel"
        assert r["date_ramasse"] == "2026-05-20"
        assert r["total_palettes"] == 5
        assert r["version"] == 1
        # Tenant scoping
        assert mock_get.call_args[0] == ("SOFRIPA", "tenant-A")


class TestCreatePrevisionnel:
    """POST /api/v1/loadings/previsionnel — délègue à send_previsionnel."""

    @patch("common.mobile_v1.verify_mobile_token")
    def test_missing_date_returns_400(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user()
        resp = client.post(
            "/api/v1/loadings/previsionnel",
            headers=auth_headers,
            json={"sscc_list": ["123456789012345678"]},
        )
        assert resp.status_code == 400

    @patch("common.mobile_v1.verify_mobile_token")
    def test_invalid_date_returns_400(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user()
        resp = client.post(
            "/api/v1/loadings/previsionnel",
            headers=auth_headers,
            json={"date_ramasse": "not-a-date", "sscc_list": []},
        )
        assert resp.status_code == 400

    @patch("common.mobile_v1.verify_mobile_token")
    def test_packaging_must_be_list(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user()
        resp = client.post(
            "/api/v1/loadings/previsionnel",
            headers=auth_headers,
            json={
                "date_ramasse": "2026-05-20",
                "sscc_list": [],
                "packaging": "not-a-list",
            },
        )
        assert resp.status_code == 400

    @patch("common.services.loading_service.send_previsionnel")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_active_ramasse_lock_returns_409(
        self, mock_verify, mock_send, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_send.side_effect = ValueError(
            "Une ramasse est déjà en cours pour SOFRIPA.",
        )
        resp = client.post(
            "/api/v1/loadings/previsionnel",
            headers=auth_headers,
            json={
                "date_ramasse": "2026-05-20",
                "sscc_list": ["111111111111111111"],
            },
        )
        assert resp.status_code == 409
        assert "déjà en cours" in resp.json()["error"]

    @patch("common.services.loading_service.send_previsionnel")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_success_returns_service_dict(
        self, mock_verify, mock_send, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_send.return_value = {
            "id": "ramasse-abc",
            "total_palettes": 3,
            "total_cartons": 288,
            "total_poids_kg": 2400,
            "inserted": 3,
            "conflicts": [],
            "email_sent": True,
            "recipients": ["exploitation@sofripa.fr", "ops@symbiose.fr"],
        }
        resp = client.post(
            "/api/v1/loadings/previsionnel",
            headers=auth_headers,
            json={
                "date_ramasse": "2026-05-20",
                "sscc_list": [
                    "111111111111111111",
                    "222222222222222222",
                    "333333333333333333",
                ],
                "packaging": [
                    {"label": "Palette Bouteilles 33cl", "qty": 2, "unit": "palette"},
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "ramasse-abc"
        assert body["total_palettes"] == 3
        assert body["email_sent"] is True
        # send_previsionnel reçoit tenant_id du token + destinataire par défaut
        call_kwargs = mock_send.call_args.kwargs
        assert mock_send.call_args[0][0] == "tenant-A"
        assert call_kwargs["destinataire"] == "SOFRIPA"
        assert call_kwargs["date_ramasse"] == _dt.date(2026, 5, 20)
        assert call_kwargs["sscc_list"] == [
            "111111111111111111",
            "222222222222222222",
            "333333333333333333",
        ]
        assert call_kwargs["user_id"] == "user-1"


class TestScanPaletteToLoading:
    """POST /api/v1/loadings/{id}/scan — lookup + link en un appel."""

    @patch("common.mobile_v1.verify_mobile_token")
    def test_missing_sscc_returns_400(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user()
        resp = client.post(
            "/api/v1/loadings/ramasse-1/scan",
            headers=auth_headers,
            json={},
        )
        assert resp.status_code == 400

    @patch("common.services.loading_service.link_palettes_to_ramasse")
    @patch("common.services.loading_service.lookup_sscc")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_ok_palette_links_and_returns_linked_true(
        self, mock_verify, mock_lookup, mock_link, client, auth_headers
    ):
        from common.services.loading_service import LookupResult

        mock_verify.return_value = _user(tenant="tenant-A")
        mock_lookup.return_value = LookupResult(
            status="ok", palette=_fake_palette(),
        )
        mock_link.return_value = (1, [])
        resp = client.post(
            "/api/v1/loadings/ramasse-1/scan",
            headers=auth_headers,
            json={"sscc": "(00)123456789012345678"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["linked"] is True
        assert body["already_in_this_loading"] is False
        assert body["palette"]["sscc"] == "123456789012345678"
        # link reçoit le tenant + la ramasse cible
        assert mock_link.call_args[0][0] == "tenant-A"
        assert mock_link.call_args.kwargs["ramasse_id"] == "ramasse-1"

    @patch("common.services.loading_service.list_linked_palettes")
    @patch("common.services.loading_service.lookup_sscc")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_already_in_this_loading_returns_idempotent_ok(
        self, mock_verify, mock_lookup, mock_list, client, auth_headers
    ):
        """Re-scan de la même palette pendant le chargement = idempotent."""
        from common.services.loading_service import LookupResult

        mock_verify.return_value = _user()
        mock_lookup.return_value = LookupResult(
            status="already_loaded",
            existing_ramasse_id="ramasse-1",  # = cette ramasse
            error_message="Palette déjà chargée",
        )
        mock_list.return_value = [_fake_palette(sscc="123456789012345678")]
        resp = client.post(
            "/api/v1/loadings/ramasse-1/scan",
            headers=auth_headers,
            json={"sscc": "123456789012345678"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["linked"] is False
        assert body["already_in_this_loading"] is True
        assert body["palette"]["sscc"] == "123456789012345678"

    @patch("common.services.loading_service.link_palettes_to_ramasse")
    @patch("common.services.loading_service.lookup_sscc")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_already_loaded_elsewhere_returns_alert(
        self, mock_verify, mock_lookup, mock_link, client, auth_headers
    ):
        """Palette liée à une AUTRE ramasse → on alerte, pas de link."""
        from common.services.loading_service import LookupResult

        mock_verify.return_value = _user()
        mock_lookup.return_value = LookupResult(
            status="already_loaded",
            existing_ramasse_id="ramasse-AUTRE",
            error_message="Palette déjà chargée sur une autre ramasse",
        )
        resp = client.post(
            "/api/v1/loadings/ramasse-1/scan",
            headers=auth_headers,
            json={"sscc": "123456789012345678"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "already_loaded"
        assert body["linked"] is False
        assert body["existing_ramasse_id"] == "ramasse-AUTRE"
        mock_link.assert_not_called()

    @patch("common.services.loading_service.lookup_sscc")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_unknown_sscc_returns_status_without_link(
        self, mock_verify, mock_lookup, client, auth_headers
    ):
        from common.services.loading_service import LookupResult

        mock_verify.return_value = _user()
        mock_lookup.return_value = LookupResult(
            status="unknown", error_message="SSCC inconnu",
        )
        resp = client.post(
            "/api/v1/loadings/ramasse-1/scan",
            headers=auth_headers,
            json={"sscc": "999999999999999999"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "unknown"
        assert body["linked"] is False


class TestFinalizeLoading:
    """POST /api/v1/loadings/{id}/finalize — délègue à finalize_loading."""

    @patch("common.services.loading_service.finalize_loading")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_not_found_returns_404(
        self, mock_verify, mock_finalize, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_finalize.side_effect = ValueError("Ramasse introuvable")
        resp = client.post(
            "/api/v1/loadings/missing/finalize", headers=auth_headers,
        )
        assert resp.status_code == 404

    @patch("common.services.loading_service.finalize_loading")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_already_definitif_returns_409(
        self, mock_verify, mock_finalize, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_finalize.side_effect = ValueError(
            "Seules les ramasses 'previsionnel' peuvent être finalisées",
        )
        resp = client.post(
            "/api/v1/loadings/r1/finalize", headers=auth_headers,
        )
        assert resp.status_code == 409

    @patch("common.services.loading_service.finalize_loading")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_success_returns_pdf_with_meta_headers(
        self, mock_verify, mock_finalize, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_finalize.return_value = (
            {
                "id": "ramasse-abc",
                "total_palettes": 5,
                "total_cartons": 480,
                "total_poids_kg": 4000,
                "email_sent": True,
                "recipients": ["exploitation@sofripa.fr"],
                "version": 2,
            },
            b"%PDF-fake-definitif",
        )
        resp = client.post(
            "/api/v1/loadings/ramasse-abc/finalize", headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == b"%PDF-fake-definitif"
        assert resp.headers["x-ramasse-id"] == "ramasse-abc"
        assert resp.headers["x-total-palettes"] == "5"
        assert resp.headers["x-email-sent"] == "true"
        assert resp.headers["x-ramasse-version"] == "2"
        # Service appelé avec tenant + ramasse_id
        assert mock_finalize.call_args[0][0] == "tenant-A"
        assert mock_finalize.call_args.kwargs["ramasse_id"] == "ramasse-abc"

    @patch("common.services.loading_service.finalize_loading")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_email_failure_still_returns_pdf(
        self, mock_verify, mock_finalize, client, auth_headers
    ):
        """Si l'envoi mail plante, on récupère quand même le PDF
        (download chauffeur prioritaire) — header X-Email-Sent à false."""
        mock_verify.return_value = _user()
        mock_finalize.return_value = (
            {
                "id": "ramasse-1",
                "total_palettes": 1,
                "total_cartons": 96,
                "total_poids_kg": 800,
                "email_sent": False,
                "recipients": [],
                "version": 2,
            },
            b"%PDF-fake",
        )
        resp = client.post(
            "/api/v1/loadings/ramasse-1/finalize", headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.headers["x-email-sent"] == "false"


class TestGetLoading:
    @patch("common.ramasse_history.get_ramasse")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_not_found_returns_404(
        self, mock_verify, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_get.return_value = None  # autre tenant ou inexistant
        resp = client.get("/api/v1/loadings/missing-id", headers=auth_headers)
        assert resp.status_code == 404
        # tenant scoping : on a bien interrogé avec le tenant du token
        assert mock_get.call_args[0][1] == "tenant-A"

    @patch("common.services.loading_service.list_linked_palettes")
    @patch("common.ramasse_history.get_ramasse")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_returns_palettes_and_totals(
        self, mock_verify, mock_get, mock_list, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_get.return_value = {
            "id": "ramasse-abc",
            "date_ramasse": _dt.date(2026, 5, 20),
            "destinataire": "FoodChéri",
            "status": "definitif",
            "total_palettes": 2,
            "total_cartons": 192,
            "total_poids_kg": 1600,
        }
        mock_list.return_value = [
            _fake_palette(sscc="111111111111111111"),
            _fake_palette(sscc="222222222222222222"),
        ]
        resp = client.get("/api/v1/loadings/ramasse-abc", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "ramasse-abc"
        assert body["destinataire"] == "FoodChéri"
        assert body["status"] == "definitif"
        assert body["date_ramasse"] == "2026-05-20"
        assert body["total_palettes"] == 2
        assert len(body["palettes"]) == 2


class TestUnlinkPalette:
    @patch("common.services.loading_service.unlink_palette")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_not_linked_returns_404(
        self, mock_verify, mock_unlink, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_unlink.return_value = False
        resp = client.delete(
            "/api/v1/loadings/ramasse-1/palettes/123456789012345678",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    @patch("common.services.loading_service.unlink_palette")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_success_returns_ok(
        self, mock_verify, mock_unlink, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_unlink.return_value = True
        resp = client.request(
            "DELETE",
            "/api/v1/loadings/ramasse-1/palettes/123456789012345678",
            headers=auth_headers,
            json={"reason": "palette cassée"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        # tenant scoping + reason propagé
        call_kwargs = mock_unlink.call_args.kwargs
        assert mock_unlink.call_args[0][0] == "tenant-A"
        assert call_kwargs["sscc"] == "123456789012345678"
        assert call_kwargs["ramasse_id"] == "ramasse-1"
        assert call_kwargs["reason"] == "palette cassée"


# ─── Historique ramasses (liste + PDF + mark driver) ────────────────────────

class TestListRamasses:
    @patch("common.ramasse_history.count_ramasses")
    @patch("common.ramasse_history.list_ramasses")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_empty_returns_empty_list_with_meta(
        self, mock_verify, mock_list, mock_count, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_list.return_value = []
        mock_count.return_value = 0
        resp = client.get("/api/v1/ramasses", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"ramasses": [], "total": 0, "limit": 20, "offset": 0}
        # tenant scoping
        assert mock_list.call_args[0][0] == "tenant-A"
        assert mock_count.call_args[0][0] == "tenant-A"

    @patch("common.ramasse_history.count_ramasses")
    @patch("common.ramasse_history.list_ramasses")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_returns_serialized_ramasses_with_pagination(
        self, mock_verify, mock_list, mock_count, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_list.return_value = [
            {
                "id": "r1",
                "date_ramasse": _dt.date(2026, 5, 16),
                "destinataire": "SOFRIPA",
                "status": "definitif",
                "total_palettes": 5,
                "total_cartons": 480,
                "total_poids_kg": 4000,
                "version": 2,
                "driver_passed": True,
                "driver_passed_at": _dt.datetime(2026, 5, 16, 14, 30, tzinfo=_dt.UTC),
                "created_at": _dt.datetime(2026, 5, 15, 18, 0, tzinfo=_dt.UTC),
            },
        ]
        mock_count.return_value = 42
        resp = client.get(
            "/api/v1/ramasses?limit=10&offset=20", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 42
        assert body["limit"] == 10
        assert body["offset"] == 20
        assert len(body["ramasses"]) == 1
        r = body["ramasses"][0]
        assert r["id"] == "r1"
        assert r["status"] == "definitif"
        assert r["date_ramasse"] == "2026-05-16"
        assert r["driver_passed"] is True
        assert r["driver_passed_at"] == "2026-05-16T14:30:00+00:00"
        assert r["has_pdf"] is True

    @patch("common.ramasse_history.count_ramasses")
    @patch("common.ramasse_history.list_ramasses")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_clamps_limit_to_max_100(
        self, mock_verify, mock_list, mock_count, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_list.return_value = []
        mock_count.return_value = 0
        resp = client.get("/api/v1/ramasses?limit=999", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["limit"] == 100
        # list_ramasses appelé avec limit clamped
        assert mock_list.call_args.kwargs["limit"] == 100


class TestRamassePdf:
    @patch("common.ramasse_history.get_ramasse")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_not_found_returns_404(
        self, mock_verify, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_get.return_value = None
        resp = client.get("/api/v1/ramasses/missing/pdf", headers=auth_headers)
        assert resp.status_code == 404
        assert mock_get.call_args[0] == ("missing", "tenant-A")

    @patch("common.ramasse_history.get_ramasse")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_no_pdf_stored_returns_404(
        self, mock_verify, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_get.return_value = {
            "id": "r1", "status": "previsionnel",
            "date_ramasse": _dt.date(2026, 5, 20),
            "version": 1,
            "pdf_bytes": None,  # PDF jamais généré (legacy ou erreur)
        }
        resp = client.get("/api/v1/ramasses/r1/pdf", headers=auth_headers)
        assert resp.status_code == 404
        assert "No PDF" in resp.json()["error"]

    @patch("common.ramasse_history.get_ramasse")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_returns_pdf_with_meta_headers(
        self, mock_verify, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_get.return_value = {
            "id": "r1",
            "status": "definitif",
            "date_ramasse": _dt.date(2026, 5, 20),
            "version": 3,
            "pdf_bytes": b"%PDF-fake-stored",
        }
        resp = client.get("/api/v1/ramasses/r1/pdf", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == b"%PDF-fake-stored"
        assert resp.headers["x-ramasse-id"] == "r1"
        assert resp.headers["x-ramasse-status"] == "definitif"
        assert resp.headers["x-ramasse-version"] == "3"
        # Filename suffixe selon statut
        assert "Definitif" in resp.headers["content-disposition"]
        assert "20260520" in resp.headers["content-disposition"]


class TestMarkDriverPassed:
    @patch("common.ramasse_history.get_ramasse")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_not_found_returns_404(
        self, mock_verify, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_get.return_value = None
        resp = client.post(
            "/api/v1/ramasses/missing/mark-driver-passed", headers=auth_headers,
        )
        assert resp.status_code == 404

    @patch("common.ramasse_history.get_ramasse")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_already_marked_returns_changed_false(
        self, mock_verify, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user()
        mock_get.return_value = {"id": "r1", "driver_passed": True}
        resp = client.post(
            "/api/v1/ramasses/r1/mark-driver-passed", headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "changed": False}

    @patch("common.ramasse_history.mark_driver_passed")
    @patch("common.ramasse_history.get_ramasse")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_marks_and_returns_changed_true(
        self, mock_verify, mock_get, mock_mark, client, auth_headers
    ):
        mock_verify.return_value = _user(tenant="tenant-A")
        mock_get.return_value = {"id": "r1", "driver_passed": False}
        mock_mark.return_value = True
        resp = client.post(
            "/api/v1/ramasses/r1/mark-driver-passed", headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "changed": True}
        # tenant + user propagés
        assert mock_mark.call_args[0][0] == "r1"
        assert mock_mark.call_args.kwargs["tenant_id"] == "tenant-A"
        assert mock_mark.call_args.kwargs["user_id"] == "user-1"


# ─── Fiches de production (admin only, beta) ────────────────────────────────

def _fake_sheet_summary(**kwargs):
    """Construit un ``ProductionSheetSummary`` minimal pour les tests."""
    from common.services.production_sheet_service import ProductionSheetSummary

    defaults = dict(
        id="sheet-1",
        brassin_id="brassin-42",
        produit="K. Mangue - Passion",
        cuve="Cuve de 7200L",
        ddm=_dt.date(2027, 5, 15),
        lot="15052027",
        status="draft",
        created_at=_dt.datetime(2026, 5, 15, 10, 0, tzinfo=_dt.UTC),
        updated_at=_dt.datetime(2026, 5, 15, 12, 0, tzinfo=_dt.UTC),
        finalized_at=None,
        created_by_email="nicolas@symbiose-kefir.fr",
    )
    defaults.update(kwargs)
    return ProductionSheetSummary(**defaults)


class TestCreateProductionSheet:
    @patch("common.mobile_v1.verify_mobile_token")
    def test_non_admin_returns_403(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="user")
        resp = client.post(
            "/api/v1/admin/production-sheets",
            headers=auth_headers, json={},
        )
        assert resp.status_code == 403

    @patch("common.mobile_v1.verify_mobile_token")
    def test_invalid_ddm_returns_400(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="admin")
        resp = client.post(
            "/api/v1/admin/production-sheets",
            headers=auth_headers,
            json={"ddm": "not-a-date"},
        )
        assert resp.status_code == 400

    @patch("common.mobile_v1.verify_mobile_token")
    def test_data_must_be_object(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="admin")
        resp = client.post(
            "/api/v1/admin/production-sheets",
            headers=auth_headers,
            json={"data": "not-a-dict"},
        )
        assert resp.status_code == 400

    @patch("common.services.production_sheet_service.create_sheet")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_success_returns_id_and_propagates_tenant_user(
        self, mock_verify, mock_create, client, auth_headers
    ):
        mock_verify.return_value = _user(role="admin", tenant="tenant-A")
        mock_create.return_value = "sheet-new-uuid"
        resp = client.post(
            "/api/v1/admin/production-sheets",
            headers=auth_headers,
            json={
                "brassin_id": "brassin-42",
                "produit": "K. Mangue - Passion",
                "cuve": "Cuve de 7200L",
                "ddm": "2027-05-15",
                "lot": "15052027",
                "data": {"fermentation": {"mesures": []}},
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"id": "sheet-new-uuid"}
        # tenant + user propagés au service (multi-tenant + audit)
        assert mock_create.call_args[0][0] == "tenant-A"
        kwargs = mock_create.call_args.kwargs
        assert kwargs["user_id"] == "user-1"
        assert kwargs["brassin_id"] == "brassin-42"
        assert kwargs["produit"] == "K. Mangue - Passion"
        assert kwargs["ddm"] == _dt.date(2027, 5, 15)
        assert kwargs["data"] == {"fermentation": {"mesures": []}}

    @patch("common.services.production_sheet_service.create_sheet")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_empty_body_creates_blank_sheet(
        self, mock_verify, mock_create, client, auth_headers
    ):
        mock_verify.return_value = _user(role="admin")
        mock_create.return_value = "sheet-blank"
        resp = client.post(
            "/api/v1/admin/production-sheets",
            headers=auth_headers, json={},
        )
        assert resp.status_code == 200
        kwargs = mock_create.call_args.kwargs
        assert kwargs["brassin_id"] is None
        assert kwargs["produit"] == ""
        assert kwargs["ddm"] is None
        assert kwargs["data"] == {}


class TestListProductionSheets:
    @patch("common.mobile_v1.verify_mobile_token")
    def test_non_admin_returns_403(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="user")
        resp = client.get(
            "/api/v1/admin/production-sheets", headers=auth_headers,
        )
        assert resp.status_code == 403

    @patch("common.services.production_sheet_service.count_sheets")
    @patch("common.services.production_sheet_service.list_sheets")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_empty_returns_empty_with_meta(
        self, mock_verify, mock_list, mock_count, client, auth_headers
    ):
        mock_verify.return_value = _user(role="admin", tenant="tenant-A")
        mock_list.return_value = []
        mock_count.return_value = 0
        resp = client.get(
            "/api/v1/admin/production-sheets", headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "sheets": [], "total": 0, "limit": 20, "offset": 0,
        }
        assert mock_list.call_args[0][0] == "tenant-A"
        assert mock_count.call_args[0][0] == "tenant-A"

    @patch("common.services.production_sheet_service.count_sheets")
    @patch("common.services.production_sheet_service.list_sheets")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_returns_serialized_sheets(
        self, mock_verify, mock_list, mock_count, client, auth_headers
    ):
        mock_verify.return_value = _user(role="admin")
        mock_list.return_value = [
            _fake_sheet_summary(id="s1", status="draft"),
            _fake_sheet_summary(
                id="s2", status="completed",
                finalized_at=_dt.datetime(2026, 5, 16, 18, 0, tzinfo=_dt.UTC),
            ),
        ]
        mock_count.return_value = 7
        resp = client.get(
            "/api/v1/admin/production-sheets?limit=5",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 7
        assert body["limit"] == 5
        assert len(body["sheets"]) == 2
        s1 = body["sheets"][0]
        assert s1["id"] == "s1"
        assert s1["status"] == "draft"
        assert s1["produit"] == "K. Mangue - Passion"
        assert s1["ddm"] == "2027-05-15"
        assert s1["finalized_at"] is None
        s2 = body["sheets"][1]
        assert s2["status"] == "completed"
        assert s2["finalized_at"] == "2026-05-16T18:00:00+00:00"

    @patch("common.services.production_sheet_service.count_sheets")
    @patch("common.services.production_sheet_service.list_sheets")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_status_filter_propagates_to_service(
        self, mock_verify, mock_list, mock_count, client, auth_headers
    ):
        mock_verify.return_value = _user(role="admin")
        mock_list.return_value = []
        mock_count.return_value = 0
        client.get(
            "/api/v1/admin/production-sheets?status=draft",
            headers=auth_headers,
        )
        # Filter status forwardé au service
        assert mock_list.call_args.kwargs["status"] == "draft"
        assert mock_count.call_args.kwargs["status"] == "draft"

    @patch("common.services.production_sheet_service.count_sheets")
    @patch("common.services.production_sheet_service.list_sheets")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_limit_clamped_to_100(
        self, mock_verify, mock_list, mock_count, client, auth_headers
    ):
        mock_verify.return_value = _user(role="admin")
        mock_list.return_value = []
        mock_count.return_value = 0
        resp = client.get(
            "/api/v1/admin/production-sheets?limit=9999",
            headers=auth_headers,
        )
        assert resp.json()["limit"] == 100
        assert mock_list.call_args.kwargs["limit"] == 100


class TestAdminBrassinsEnCours:
    @patch("common.mobile_v1.verify_mobile_token")
    def test_non_admin_returns_403(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="user")
        resp = client.get(
            "/api/v1/admin/brassins-en-cours", headers=auth_headers,
        )
        assert resp.status_code == 403

    @patch("common.services.ramasse_service.load_active_brassins")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_returns_brassins_and_errors(
        self, mock_verify, mock_load, client, auth_headers
    ):
        from common.easybeer.models import BrassinLight

        mock_verify.return_value = _user(role="admin")
        mock_load.return_value = (
            [
                BrassinLight(
                    id_brassin=42, nom="B042", volume=7200.0, annule=False,
                    produit_libelle="Kéfir Mangue Passion", id_produit=12,
                    is_archive=False, raw={},
                ),
                BrassinLight(
                    id_brassin=41, nom="B041", volume=3600.0, annule=False,
                    produit_libelle="Kombucha Gingembre", id_produit=20,
                    is_archive=True, raw={},
                ),
            ],
            ["Brassins archivés : timeout"],
        )
        resp = client.get(
            "/api/v1/admin/brassins-en-cours", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["brassins"]) == 2
        assert body["brassins"][0]["id_brassin"] == 42
        assert body["brassins"][0]["produit_libelle"] == "Kéfir Mangue Passion"
        assert body["brassins"][1]["is_archive"] is True
        assert "timeout" in body["errors"][0]


class TestAdminConditionnementByLot:
    @patch("common.mobile_v1.verify_mobile_token")
    def test_non_admin_returns_403(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="user")
        resp = client.get(
            "/api/v1/admin/conditionnement-by-lot?lot=15052027",
            headers=auth_headers,
        )
        assert resp.status_code == 403

    @patch("common.mobile_v1.verify_mobile_token")
    def test_missing_lot_returns_400(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="admin")
        resp = client.get(
            "/api/v1/admin/conditionnement-by-lot", headers=auth_headers,
        )
        assert resp.status_code == 400

    @patch(
        "common.services.production_sheet_service.compute_real_conditionnement_by_lot",
    )
    @patch("common.mobile_v1.verify_mobile_token")
    def test_returns_aggregated_items_with_tenant_propagated(
        self, mock_verify, mock_compute, client, auth_headers
    ):
        from common.services.production_sheet_service import (
            ConditionnementByLot,
            ConditionnementLine,
        )

        mock_verify.return_value = _user(role="admin", tenant="tenant-A")
        mock_compute.return_value = ConditionnementByLot(
            lot="15052027",
            items=[
                ConditionnementLine(
                    fmt="12x33", marque="SYMBIOSE",
                    designation="K. Mangue - Passion",
                    cartons=843, palettes=12,
                ),
                ConditionnementLine(
                    fmt="6x75", marque="SYMBIOSE",
                    designation="K. Mangue - Passion",
                    cartons=347, palettes=4,
                ),
            ],
            total_cartons=1190,
            total_palettes=16,
        )
        resp = client.get(
            "/api/v1/admin/conditionnement-by-lot?lot=15052027",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lot"] == "15052027"
        assert body["total_cartons"] == 1190
        assert body["total_palettes"] == 16
        assert len(body["items"]) == 2
        item = body["items"][0]
        assert item["fmt"] == "12x33"
        assert item["marque"] == "SYMBIOSE"
        assert item["cartons"] == 843
        assert item["palettes"] == 12
        # tenant_id du token propagé au service
        assert mock_compute.call_args[0][0] == "tenant-A"
        assert mock_compute.call_args[0][1] == "15052027"


def _fake_sheet_detail(**kwargs):
    """Construit un ProductionSheetDetail pour les tests GET/PATCH."""
    from common.services.production_sheet_service import ProductionSheetDetail

    defaults = dict(
        id="sheet-1",
        brassin_id="brassin-42",
        produit="K. Mangue - Passion",
        cuve="Cuve de 7200L",
        ddm=_dt.date(2027, 5, 15),
        lot="15052027",
        status="draft",
        data={"fermentation": {"mesures": []}},
        created_at=_dt.datetime(2026, 5, 15, 10, 0, tzinfo=_dt.UTC),
        updated_at=_dt.datetime(2026, 5, 15, 12, 0, tzinfo=_dt.UTC),
        finalized_at=None,
        created_by_email="nicolas@symbiose-kefir.fr",
    )
    defaults.update(kwargs)
    return ProductionSheetDetail(**defaults)


class TestGetProductionSheet:
    @patch("common.mobile_v1.verify_mobile_token")
    def test_non_admin_returns_403(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="user")
        resp = client.get(
            "/api/v1/admin/production-sheets/sheet-1", headers=auth_headers,
        )
        assert resp.status_code == 403

    @patch("common.services.production_sheet_service.get_sheet")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_not_found_returns_404(
        self, mock_verify, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user(role="admin", tenant="tenant-A")
        mock_get.return_value = None
        resp = client.get(
            "/api/v1/admin/production-sheets/missing", headers=auth_headers,
        )
        assert resp.status_code == 404
        # tenant_id propagé
        assert mock_get.call_args[0] == ("tenant-A", "missing")

    @patch("common.services.production_sheet_service.get_sheet")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_returns_full_serialized_detail(
        self, mock_verify, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user(role="admin")
        mock_get.return_value = _fake_sheet_detail()
        resp = client.get(
            "/api/v1/admin/production-sheets/sheet-1", headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "sheet-1"
        assert body["produit"] == "K. Mangue - Passion"
        assert body["ddm"] == "2027-05-15"
        assert body["status"] == "draft"
        # data JSONB renvoyé tel quel
        assert body["data"] == {"fermentation": {"mesures": []}}


class TestPatchProductionSheet:
    @patch("common.mobile_v1.verify_mobile_token")
    def test_non_admin_returns_403(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="user")
        resp = client.request(
            "PATCH",
            "/api/v1/admin/production-sheets/sheet-1",
            headers=auth_headers, json={"produit": "X"},
        )
        assert resp.status_code == 403

    @patch("common.mobile_v1.verify_mobile_token")
    def test_invalid_ddm_returns_400(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="admin")
        resp = client.request(
            "PATCH",
            "/api/v1/admin/production-sheets/sheet-1",
            headers=auth_headers, json={"ddm": "nope"},
        )
        assert resp.status_code == 400

    @patch("common.mobile_v1.verify_mobile_token")
    def test_data_must_be_object(self, mock_verify, client, auth_headers):
        mock_verify.return_value = _user(role="admin")
        resp = client.request(
            "PATCH",
            "/api/v1/admin/production-sheets/sheet-1",
            headers=auth_headers, json={"data": "not-a-dict"},
        )
        assert resp.status_code == 400

    @patch("common.services.production_sheet_service.patch_sheet")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_not_found_returns_404(
        self, mock_verify, mock_patch, client, auth_headers
    ):
        mock_verify.return_value = _user(role="admin")
        mock_patch.return_value = False
        resp = client.request(
            "PATCH",
            "/api/v1/admin/production-sheets/missing",
            headers=auth_headers, json={"produit": "X"},
        )
        assert resp.status_code == 404

    @patch("common.services.production_sheet_service.get_sheet")
    @patch("common.services.production_sheet_service.patch_sheet")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_success_returns_updated_sheet(
        self, mock_verify, mock_patch, mock_get, client, auth_headers
    ):
        mock_verify.return_value = _user(role="admin", tenant="tenant-A")
        mock_patch.return_value = True
        mock_get.return_value = _fake_sheet_detail(
            produit="K. Pêche", ddm=_dt.date(2027, 6, 1),
        )
        resp = client.request(
            "PATCH",
            "/api/v1/admin/production-sheets/sheet-1",
            headers=auth_headers,
            json={
                "produit": "K. Pêche",
                "ddm": "2027-06-01",
                "data": {"fermentation": {"statut": "Conforme"}},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["sheet"]["produit"] == "K. Pêche"
        assert body["sheet"]["ddm"] == "2027-06-01"
        # PATCH appelé avec tenant_id + les bons kwargs
        assert mock_patch.call_args[0][0] == "tenant-A"
        assert mock_patch.call_args[0][1] == "sheet-1"
        kwargs = mock_patch.call_args.kwargs
        assert kwargs["produit"] == "K. Pêche"
        assert kwargs["ddm"] == _dt.date(2027, 6, 1)
        assert kwargs["data"] == {"fermentation": {"statut": "Conforme"}}

    @patch("common.services.production_sheet_service.get_sheet")
    @patch("common.services.production_sheet_service.patch_sheet")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_only_sends_provided_fields(
        self, mock_verify, mock_patch, mock_get, client, auth_headers
    ):
        """PATCH sémantique : si on n'envoie que `produit`, le service ne
        reçoit QUE produit (le reste reste à sa valeur DB)."""
        mock_verify.return_value = _user(role="admin")
        mock_patch.return_value = True
        mock_get.return_value = _fake_sheet_detail()
        client.request(
            "PATCH",
            "/api/v1/admin/production-sheets/sheet-1",
            headers=auth_headers,
            json={"produit": "Nouveau produit"},
        )
        kwargs = mock_patch.call_args.kwargs
        # Seul produit est fourni — pas de cuve, ddm, lot, data dans kwargs
        assert "produit" in kwargs
        assert "cuve" not in kwargs
        assert "ddm" not in kwargs
        assert "data" not in kwargs

    @patch("common.services.production_sheet_service.get_sheet")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_empty_body_returns_current_sheet(
        self, mock_verify, mock_get, client, auth_headers
    ):
        """PATCH sans body → idempotent, renvoie l'état actuel sans toucher."""
        mock_verify.return_value = _user(role="admin")
        mock_get.return_value = _fake_sheet_detail()
        resp = client.request(
            "PATCH",
            "/api/v1/admin/production-sheets/sheet-1",
            headers=auth_headers, json={},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["sheet"]["id"] == "sheet-1"
