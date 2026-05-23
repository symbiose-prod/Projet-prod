"""Tests for common/services/loading_eb_bind.py — finalize ramasse → EB bind."""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from common.services.loading_eb_bind import (
    _coerce_date_formulaire,
    _get_sofripa_client_id,
    _get_warehouse_id,
    _normalize_gtin,
    build_gtin_to_id_produit_index,
    build_stock_sortie_payload,
    enqueue_eb_events_from_loading,
    is_eb_bind_enabled,
)

# ─── Fake PaletteInfo ────────────────────────────────────────────────────


@dataclass
class _FakePalette:
    sscc: str = "377001442700000001"
    gtin_palette: str = "3770014427014"
    lot: str = "LOT-2026-05"
    case_count: int = 10
    gtin_uvc: str = ""
    ddm: str = "2026-09-01"
    designation: str = "Kéfir Pêche"
    fmt: str = "12x33"
    marque: str = "Ferment"
    gout: str = "Pêche"
    pcb: int = 12


# ─── Feature flag ────────────────────────────────────────────────────────


class TestIsEbBindEnabled:

    @patch.dict("os.environ", {}, clear=True)
    def test_off_by_default(self):
        assert is_eb_bind_enabled() is False

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_LOADINGS": "true"})
    def test_enabled_with_true(self):
        assert is_eb_bind_enabled() is True

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_LOADINGS": "1"})
    def test_enabled_with_one(self):
        assert is_eb_bind_enabled() is True

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_LOADINGS": "false"})
    def test_explicit_false(self):
        assert is_eb_bind_enabled() is False


# ─── Config lookups ──────────────────────────────────────────────────────


class TestGetWarehouseId:

    @patch.dict("os.environ", {}, clear=True)
    def test_none_when_unset(self):
        assert _get_warehouse_id() is None

    @patch.dict("os.environ", {"EB_DEFAULT_WAREHOUSE_ID": "42"})
    def test_returns_int(self):
        assert _get_warehouse_id() == 42

    @patch.dict("os.environ", {"EB_DEFAULT_WAREHOUSE_ID": "not-an-int"})
    def test_invalid_returns_none(self):
        assert _get_warehouse_id() is None


class TestGetSofripaClientId:

    @patch.dict("os.environ", {}, clear=True)
    def test_none_when_unset(self):
        assert _get_sofripa_client_id() is None

    @patch.dict("os.environ", {"EB_SOFRIPA_CLIENT_ID": "1234"})
    def test_returns_int(self):
        assert _get_sofripa_client_id() == 1234


# ─── GTIN normalization ──────────────────────────────────────────────────


class TestNormalizeGtin:

    def test_strips_non_digits(self):
        assert _normalize_gtin(" 3770 014427014 ") == "3770014427014"

    def test_empty(self):
        assert _normalize_gtin("") == ""
        assert _normalize_gtin(None) == ""

    def test_only_letters(self):
        assert _normalize_gtin("ABC") == ""


# ─── Index GTIN → idProduit ──────────────────────────────────────────────


class TestBuildGtinIndex:

    def test_empty_matrice(self):
        assert build_gtin_to_id_produit_index({}) == {}

    def test_builds_index_from_matrice(self):
        matrice = {
            "produits": [
                {
                    "codesBarres": [
                        {
                            "code": "3770014427014",
                            "modeleProduit": {"idProduit": 100},
                        },
                        {
                            "code": "3770014427021",
                            "modeleProduit": {"idProduit": 101},
                        },
                    ],
                }
            ]
        }
        index = build_gtin_to_id_produit_index(matrice)
        assert index["3770014427014"] == 100
        assert index["427014"] == 100  # 6 derniers digits aussi indexés
        assert index["3770014427021"] == 101

    def test_skips_entries_without_id_produit(self):
        matrice = {"produits": [{"codesBarres": [{"code": "123456"}]}]}
        assert build_gtin_to_id_produit_index(matrice) == {}


# ─── Builder payload ─────────────────────────────────────────────────────


class TestBuildStockSortiePayload:

    def test_single_palette(self):
        palettes = [_FakePalette()]
        gtin_index = {"3770014427014": 100}

        payload, warnings = build_stock_sortie_payload(
            palettes=palettes,
            gtin_to_id_produit=gtin_index,
            id_entrepot=1,
            id_client=999,
            date_ramasse="2026-05-23",
            ramasse_numero=42,
            destinataire="SOFRIPA",
        )

        assert warnings == []
        assert len(payload["elements"]) == 1
        el = payload["elements"][0]
        assert el["produit"]["idProduit"] == 100
        assert el["entrepot"]["idEntrepot"] == 1
        assert el["quantite"] == 10
        assert el["modeleNumerosLots"][0]["numeroLot"] == "LOT-2026-05"
        assert "Ramasse" in payload["libelle"]
        assert "#42" in payload["libelle"]
        assert "SOFRIPA" in payload["libelle"]
        assert payload["dateFormulaire"].startswith("2026-05-23")

    def test_multiple_palettes_same_product(self):
        palettes = [
            _FakePalette(sscc="A" * 18, lot="L1", case_count=10),
            _FakePalette(sscc="B" * 18, lot="L2", case_count=8),
        ]
        gtin_index = {"3770014427014": 100}

        payload, warnings = build_stock_sortie_payload(
            palettes=palettes,
            gtin_to_id_produit=gtin_index,
            id_entrepot=1,
            id_client=999,
            date_ramasse="2026-05-23",
            ramasse_numero=None,
            destinataire="SOFRIPA",
        )

        assert warnings == []
        # 1 element par palette (granularité fine pour traçabilité lot)
        assert len(payload["elements"]) == 2
        lots = [el["modeleNumerosLots"][0]["numeroLot"] for el in payload["elements"]]
        assert "L1" in lots
        assert "L2" in lots

    def test_unmapped_gtin_skipped_with_warning(self):
        palettes = [_FakePalette(gtin_palette="9999999999999")]
        gtin_index = {"3770014427014": 100}

        payload, warnings = build_stock_sortie_payload(
            palettes=palettes,
            gtin_to_id_produit=gtin_index,
            id_entrepot=1,
            id_client=999,
            date_ramasse="2026-05-23",
            ramasse_numero=None,
            destinataire="SOFRIPA",
        )

        assert payload["elements"] == []
        assert len(warnings) == 1
        assert "idProduit introuvable" in warnings[0]

    def test_no_gtin_skipped_with_warning(self):
        palettes = [_FakePalette(gtin_palette="", gtin_uvc="")]
        payload, warnings = build_stock_sortie_payload(
            palettes=palettes,
            gtin_to_id_produit={},
            id_entrepot=1,
            id_client=999,
            date_ramasse="2026-05-23",
            ramasse_numero=None,
            destinataire="SOFRIPA",
        )
        assert payload["elements"] == []
        assert any("pas de GTIN" in w for w in warnings)

    def test_lot_optional_in_element(self):
        palettes = [_FakePalette(lot="")]
        gtin_index = {"3770014427014": 100}

        payload, _ = build_stock_sortie_payload(
            palettes=palettes,
            gtin_to_id_produit=gtin_index,
            id_entrepot=1,
            id_client=999,
            date_ramasse="2026-05-23",
            ramasse_numero=None,
            destinataire="",
        )
        assert "modeleNumerosLots" not in payload["elements"][0]

    @patch.dict("os.environ", {"EB_DEFAULT_SORTIE_TYPE_ID": "5"})
    def test_optional_sortie_type(self):
        payload, _ = build_stock_sortie_payload(
            palettes=[_FakePalette()],
            gtin_to_id_produit={"3770014427014": 100},
            id_entrepot=1,
            id_client=999,
            date_ramasse="2026-05-23",
            ramasse_numero=None,
            destinataire="",
        )
        assert payload["type"]["idStockSortieType"] == 5


# ─── Coerce date ─────────────────────────────────────────────────────────


class TestCoerceDate:

    def test_iso_date(self):
        assert _coerce_date_formulaire("2026-05-23") == "2026-05-23T12:00:00"

    def test_invalid_date_falls_back_to_now(self):
        result = _coerce_date_formulaire("garbage")
        assert "T" in result
        # YYYY-MM-DDTHH:MM:SS
        assert len(result) >= 19


# ─── enqueue_eb_events_from_loading (intégration) ────────────────────────


class TestEnqueueEbEventsFromLoading:

    @patch.dict("os.environ", {}, clear=True)
    def test_flag_off(self):
        result = enqueue_eb_events_from_loading(
            palettes=[_FakePalette()],
            ramasse_id="r-1",
            ramasse_numero=1,
            date_ramasse="2026-05-23",
            destinataire="SOFRIPA",
            tenant_id="t1",
            user_email="x@y.com",
        )
        assert result["enabled"] is False
        assert "not enabled" in result["skipped_reason"]

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_LOADINGS": "true"})
    def test_no_palettes(self):
        result = enqueue_eb_events_from_loading(
            palettes=[],
            ramasse_id="r-1",
            ramasse_numero=1,
            date_ramasse="2026-05-23",
            destinataire="SOFRIPA",
            tenant_id="t1",
            user_email="x@y.com",
        )
        assert "no palettes" in result["skipped_reason"]

    @patch.dict("os.environ", {"EB_OUTBOX_BIND_LOADINGS": "true"}, clear=True)
    def test_missing_env_config(self):
        """Flag activé mais warehouse/client non configurés → skip avec raison."""
        result = enqueue_eb_events_from_loading(
            palettes=[_FakePalette()],
            ramasse_id="r-1",
            ramasse_numero=1,
            date_ramasse="2026-05-23",
            destinataire="SOFRIPA",
            tenant_id="t1",
            user_email="x@y.com",
        )
        assert "non configuré" in result["skipped_reason"]

    @patch.dict("os.environ", {
        "EB_OUTBOX_BIND_LOADINGS": "true",
        "EB_DEFAULT_WAREHOUSE_ID": "1",
        "EB_SOFRIPA_CLIENT_ID": "999",
    })
    @patch("common.easybeer.queued.enqueue_stock_sortie")
    @patch("common.easybeer.conditioning.get_code_barre_matrice")
    def test_full_happy_path(
        self,
        mock_matrice: MagicMock,
        mock_enqueue: MagicMock,
    ):
        mock_matrice.return_value = {
            "produits": [
                {
                    "codesBarres": [
                        {"code": "3770014427014", "modeleProduit": {"idProduit": 100}},
                    ],
                }
            ]
        }
        mock_enqueue.return_value = 77

        result = enqueue_eb_events_from_loading(
            palettes=[_FakePalette()],
            ramasse_id="r-1",
            ramasse_numero=42,
            date_ramasse="2026-05-23",
            destinataire="SOFRIPA",
            tenant_id="t1",
            user_email="user@x.com",
        )

        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        assert kwargs["tenant_id"] == "t1"
        assert kwargs["user_email"] == "user@x.com"
        assert len(kwargs["payload"]["elements"]) == 1

        assert any(
            e["event_type"] == "stock.sortie" and e["id"] == 77
            for e in result["enqueued"]
        )

    @patch.dict("os.environ", {
        "EB_OUTBOX_BIND_LOADINGS": "true",
        "EB_DEFAULT_WAREHOUSE_ID": "1",
        "EB_SOFRIPA_CLIENT_ID": "999",
    })
    @patch("common.easybeer.conditioning.get_code_barre_matrice", side_effect=RuntimeError("EB down"))
    def test_matrice_failure_caught(self, _mock_matrice: MagicMock):
        result = enqueue_eb_events_from_loading(
            palettes=[_FakePalette()],
            ramasse_id="r-1",
            ramasse_numero=42,
            date_ramasse="2026-05-23",
            destinataire="SOFRIPA",
            tenant_id="t1",
            user_email="x@y.com",
        )
        assert any("matrice" in err for err in result["errors"])

    @patch.dict("os.environ", {
        "EB_OUTBOX_BIND_LOADINGS": "true",
        "EB_DEFAULT_WAREHOUSE_ID": "1",
        "EB_SOFRIPA_CLIENT_ID": "999",
    })
    @patch("common.easybeer.conditioning.get_code_barre_matrice")
    def test_no_element_after_mapping_skipped(self, mock_matrice: MagicMock):
        """Si aucun GTIN ne matche, on skip l'enqueue avec raison."""
        mock_matrice.return_value = {
            "produits": [
                {"codesBarres": [{"code": "9999999999999", "modeleProduit": {"idProduit": 1}}]}
            ]
        }
        result = enqueue_eb_events_from_loading(
            palettes=[_FakePalette(gtin_palette="3770014427014")],
            ramasse_id="r-1",
            ramasse_numero=42,
            date_ramasse="2026-05-23",
            destinataire="SOFRIPA",
            tenant_id="t1",
            user_email="x@y.com",
        )
        assert "tous gtin non mappés" in result["skipped_reason"]
