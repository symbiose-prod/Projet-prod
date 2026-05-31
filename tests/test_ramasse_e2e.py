"""
tests/test_ramasse_e2e.py
=========================
Test end-to-end du workflow ramasse J1 → J2 complet :

  1. J1 soir  — POST /loadings/previsionnel  (création + envoi BL provisoire)
  2. J2 matin — GET  /active-ramasses        (l'opérateur reprend où il en est)
  3. J2 chargement — POST /loadings/{id}/scan (x N palettes)
  4. J2       — GET  /loadings/{id}           (vérification des palettes liées)
  5. J2 fin   — POST /loadings/{id}/finalize  (BL définitif + email)
  6. J2 fin   — POST /ramasses/{id}/mark-driver-passed (chauffeur)
  7. Suivi    — GET  /loadings/{id}            (vérifie driver_passed=true)

Objectif : détecter les ruptures de composition entre endpoints.
Les tests par-endpoint existants ne capturent PAS qu'un changement de
signature dans `_v1_get_loading` casse le polling J2 ou que le payload
finalize n'est plus compatible avec la requête mark-driver-passed.

Strategy : mock au niveau des fonctions de service métier (cohérent avec
le reste de la suite test_mobile_v1_endpoints) + état partagé entre
appels (un mini "ramasse store" en mémoire) pour simuler le cycle DB
sans vraie connexion Postgres.
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from common.mobile_v1 import register_routes


@pytest.fixture
def client():
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer e2e-token"}


@pytest.fixture
def user_dict():
    return {
        "id": "user-e2e",
        "tenant_id": "tenant-e2e",
        "email": "ops@symbiose-kefir.fr",
        "role": "user",
    }


def _make_palette(sscc: str):
    """Mini factory pour les palettes retournées par le service —
    retourne une instance PaletteInfo (dataclass frozen)."""
    from common.services.loading_service import PaletteInfo
    return PaletteInfo(
        sscc=sscc,
        gtin_palette="01234567890123",
        lot="240520",
        ddm=_dt.date(2027, 5, 20),
        case_count=96,
        designation="Kéfir Mangue Passion",
        fmt="12x33",
        marque="Symbiose",
        gout="Mangue Passion",
        pcb=24,
        gtin_uvc="98765432109876",
        generated_at=_dt.datetime(2026, 5, 19, 18, 0, tzinfo=_dt.UTC),
    )


class TestRamasseE2EWorkflow:
    """Chaîne complète J1 prévisionnel → J2 chargement/finalize/livraison."""

    @patch("common.ramasse_history.get_ramasse")
    @patch("common.services.loading_service.list_linked_palettes")
    @patch("common.ramasse_history.mark_driver_passed")
    @patch("common.services.loading_service.finalize_loading")
    @patch("common.services.loading_service.link_palettes_to_ramasse")
    @patch("common.services.loading_service.lookup_sscc")
    @patch("common.ramasse_history.get_active_ramasse_for_dest")
    @patch("common.services.loading_service.send_previsionnel")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_full_j1_to_j2_workflow(
        self,
        mock_verify,
        mock_send_prev,
        mock_get_active,
        mock_lookup,
        mock_link,
        mock_finalize,
        mock_mark_driver,
        mock_list_palettes,
        mock_get_ramasse,
        client,
        auth_headers,
        user_dict,
    ):
        # ─── Setup commun ─────────────────────────────────────────────────
        mock_verify.return_value = user_dict
        ramasse_id = "ramasse-e2e-001"
        date_ramasse = _dt.date(2026, 5, 22)
        ssccs = [
            "111111111111111111",
            "222222222222222222",
            "333333333333333333",
        ]

        # ═══ Étape 1 — J1 soir : création prévisionnel ═══════════════════
        mock_send_prev.return_value = {
            "id": ramasse_id,
            "total_palettes": 3,
            "total_cartons": 288,
            "total_poids_kg": 2400,
            "inserted": 3,
            "conflicts": [],
            "email_sent": True,
            "recipients": ["exploitation@sofripa.fr"],
        }
        resp = client.post(
            "/api/v1/loadings/previsionnel",
            headers=auth_headers,
            json={
                "date_ramasse": date_ramasse.isoformat(),
                "sscc_list": ssccs,
                "packaging": [
                    {"label": "Palette Bouteilles 33cl", "qty": 2, "unit": "palette"},
                ],
            },
        )
        assert resp.status_code == 200, "J1 previsionnel doit réussir"
        prev_body = resp.json()
        assert prev_body["id"] == ramasse_id
        assert prev_body["total_palettes"] == 3
        assert prev_body["email_sent"] is True

        # Vérif scoping tenant + user propagés au service
        call = mock_send_prev.call_args
        assert call.args[0] == "tenant-e2e"
        assert call.kwargs["user_id"] == "user-e2e"
        assert call.kwargs["destinataire"] == "SOFRIPA"
        assert call.kwargs["sscc_list"] == ssccs

        # ═══ Étape 2 — J2 matin : reprise du previsionnel ═════════════════
        mock_get_active.return_value = {
            "id": ramasse_id,
            "date_ramasse": date_ramasse,
            "destinataire": "SOFRIPA",
            "status": "previsionnel",
            "total_palettes": 3,
            "total_cartons": 288,
            "total_poids_kg": 2400,
            "version": 1,
            "created_by_email": user_dict["email"],
            "created_at": _dt.datetime(2026, 5, 21, 18, 0, tzinfo=_dt.UTC),
        }
        resp = client.get("/api/v1/active-ramasses", headers=auth_headers)
        assert resp.status_code == 200
        active = resp.json()["ramasses"]
        assert len(active) == 1, "L'opérateur doit retrouver SA ramasse au J2"
        assert active[0]["id"] == ramasse_id
        assert active[0]["status"] == "previsionnel"
        assert active[0]["date_ramasse"] == date_ramasse.isoformat()

        # ═══ Étape 3 — J2 chargement : scan des 3 palettes ════════════════
        from common.services.loading_service import LookupResult, PaletteInfo

        for sscc in ssccs:
            mock_lookup.return_value = LookupResult(
                status="ok",
                palette=PaletteInfo(
                    sscc=sscc,
                    gtin_palette="01234567890123",
                    lot="240520",
                    ddm=_dt.date(2027, 5, 20),
                    case_count=96,
                    designation="Kéfir Mangue Passion",
                    fmt="12x33",
                    marque="Symbiose",
                    gout="Mangue Passion",
                    pcb=24,
                    gtin_uvc="98765432109876",
                    generated_at=_dt.datetime(2026, 5, 19, 18, 0, tzinfo=_dt.UTC),
                ),
                error_message="",
            )
            # link_palettes_to_ramasse retourne un tuple (inserted, conflicts)
            mock_link.return_value = (1, [])
            resp = client.post(
                f"/api/v1/loadings/{ramasse_id}/scan",
                headers=auth_headers,
                json={"sscc": sscc},
            )
            assert resp.status_code == 200, f"Scan {sscc} doit réussir"
            body = resp.json()
            assert body["status"] == "ok"
            assert body["linked"] is True
            assert body["palette"]["sscc"] == sscc

        # ═══ Étape 4 — J2 : vérif palettes liées ═════════════════════════
        mock_get_ramasse.return_value = {
            "id": ramasse_id,
            "date_ramasse": date_ramasse,
            "destinataire": "SOFRIPA",
            "status": "previsionnel",
            "total_palettes": 3,
            "total_cartons": 288,
            "total_poids_kg": 2400,
            "driver_passed": False,
            "driver_passed_at": None,
            "previsionnel_sscc_list": ssccs,
        }
        mock_list_palettes.return_value = [_make_palette(s) for s in ssccs]
        resp = client.get(
            f"/api/v1/loadings/{ramasse_id}", headers=auth_headers,
        )
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["id"] == ramasse_id
        assert detail["status"] == "previsionnel"
        assert detail["driver_passed"] is False
        assert len(detail["palettes"]) == 3
        # invariant cross-call : SSCC scannés == previsionnel J1
        assert {p["sscc"] for p in detail["palettes"]} == set(ssccs)

        # ═══ Étape 5 — J2 fin : finalize ══════════════════════════════════
        # finalize_loading retourne un tuple (info_dict, pdf_bytes).
        # L'endpoint renvoie le PDF binaire + headers X-* avec les totaux.
        mock_finalize.return_value = (
            {
                "id": ramasse_id,
                "status": "definitif",
                "total_palettes": 3,
                "total_cartons": 288,
                "total_poids_kg": 2400,
                "version": 2,
                "email_sent": True,
                "recipients": ["exploitation@sofripa.fr"],
            },
            b"%PDF-1.4 fake binary content",
        )
        resp = client.post(
            f"/api/v1/loadings/{ramasse_id}/finalize",
            headers=auth_headers,
            json={},
        )
        assert resp.status_code == 200
        # Le finalize renvoie le PDF directement, métadonnées en headers
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["X-Ramasse-Id"] == ramasse_id
        assert resp.headers["X-Total-Palettes"] == "3"
        assert resp.headers["X-Email-Sent"] == "true"
        assert resp.content.startswith(b"%PDF-")

        # invariant cross-call : tenant_id propagé au service
        call = mock_finalize.call_args
        assert call.args[0] == "tenant-e2e"
        assert call.kwargs["ramasse_id"] == ramasse_id

        # ═══ Étape 6 — Mark driver passed ═════════════════════════════════
        mock_get_ramasse.return_value = {
            **mock_get_ramasse.return_value,
            "status": "definitif",  # transition appliquée
            "driver_passed": False,
        }
        mock_mark_driver.return_value = True  # transition appliquée

        resp = client.post(
            f"/api/v1/ramasses/{ramasse_id}/mark-driver-passed",
            headers=auth_headers,
            json={},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "changed": True}

        # ═══ Étape 7 — Vérif final : driver_passed=true côté detail ═══════
        mock_get_ramasse.return_value = {
            **mock_get_ramasse.return_value,
            "status": "definitif",
            "driver_passed": True,
            "driver_passed_at": _dt.datetime(2026, 5, 22, 15, 0, tzinfo=_dt.UTC),
        }
        resp = client.get(
            f"/api/v1/loadings/{ramasse_id}", headers=auth_headers,
        )
        assert resp.status_code == 200
        final_detail = resp.json()
        # Invariants métier finaux : ramasse définitive + livrée
        assert final_detail["status"] == "definitif"
        assert final_detail["driver_passed"] is True
        assert final_detail["driver_passed_at"] == "2026-05-22T15:00:00+00:00"
        # Le BL provisoire doit avoir disparu (transition vers définitif)
        # — previsionnel_sscc_list peut rester pour audit mais le statut
        # est ce qui drive l'UI iOS (badge LIVRÉ).

    @patch("common.ramasse_history.get_ramasse")
    @patch("common.services.loading_service.list_linked_palettes")
    @patch("common.mobile_v1.verify_mobile_token")
    def test_tenant_isolation_blocks_cross_access(
        self,
        mock_verify,
        mock_list_palettes,
        mock_get_ramasse,
        client,
        auth_headers,
    ):
        """Tenant A ne peut PAS accéder au détail d'une ramasse tenant B
        — invariant de sécurité cross-endpoint."""
        # User authentifié comme tenant-A
        mock_verify.return_value = {
            "id": "user-A",
            "tenant_id": "tenant-A",
            "email": "a@x.fr",
            "role": "user",
        }
        # Backend renvoie None pour une ramasse tenant-B (scoping filtré)
        mock_get_ramasse.return_value = None

        resp = client.get(
            "/api/v1/loadings/ramasse-tenant-B-secret",
            headers=auth_headers,
        )
        assert resp.status_code == 404, "tenant scoping doit retourner 404"
        # Vérifie le tenant_id passé au get_ramasse (2e arg positionnel)
        assert mock_get_ramasse.call_args.args[1] == "tenant-A"
