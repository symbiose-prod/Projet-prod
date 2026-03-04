"""Tests for common/brassin_builder.py — EasyBeer brassin creation business logic."""
from __future__ import annotations

import math

from common.brassin_builder import (
    _norm_etape,
    build_brassin_payload,
    build_etape_planification,
    generate_brassin_code,
    match_contenant_id,
    parse_derive_map,
    parse_packaging_lookup,
    scale_recipe_ingredients,
)

# ─── generate_brassin_code ────────────────────────────────────────────────────


class TestGenerateBrassinCode:

    def test_kefir_prefix(self):
        code = generate_brassin_code("Original", "2026-03-10", "Kéfir Original")
        assert code.startswith("K")

    def test_kefir_uses_first_two_chars_uppercased(self):
        code = generate_brassin_code("mangue", "2026-03-10", "Kéfir Mangue")
        assert code == "KMA10032026"

    def test_infusion_prefix(self):
        code = generate_brassin_code("Citron", "2026-03-10", "Infusion Probiotique Citron")
        assert code.startswith("IP")

    def test_infusion_uses_first_char_uppercased(self):
        code = generate_brassin_code("citron", "2026-03-10", "Infusion Probiotique Citron")
        assert code == "IPC10032026"

    def test_infusion_detection_case_insensitive(self):
        code = generate_brassin_code("Rose", "2026-01-05", "INFUSION rose")
        assert code.startswith("IP")
        assert code == "IPR05012026"

    def test_date_formatting_ddmmyyyy(self):
        code = generate_brassin_code("Or", "2026-12-25", "Kéfir Original")
        assert code.endswith("25122026")

    def test_empty_gout_kefir(self):
        code = generate_brassin_code("", "2026-03-10", "Kéfir Original")
        # First two chars of "" is "" — code is "K" + "" + date
        assert code == "K10032026"

    def test_single_char_gout_kefir(self):
        code = generate_brassin_code("X", "2026-06-01", "Kéfir X")
        # gout[:2] on single char → "X"
        assert code == "KX01062026"


# ─── build_brassin_payload ────────────────────────────────────────────────────


class TestBuildBrassinPayload:

    def test_all_required_fields_present(self):
        p = build_brassin_payload(
            code="KOR10032026",
            vol_l=7200.0,
            perte_litres=360.0,
            semaine_du="2026-03-10",
            date_embout_iso="2026-03-24",
            id_produit=42,
        )
        assert p["nom"] == "KOR10032026"
        assert p["volume"] == 7200.0
        assert p["produit"] == {"idProduit": 42}
        assert p["type"] == {"code": "LOCALE"}
        assert p["deduireMatierePremiere"] is True
        assert p["changementEtapeAutomatique"] is True

    def test_percentage_calculation(self):
        p = build_brassin_payload(
            code="T", vol_l=1000.0, perte_litres=50.0,
            semaine_du="2026-01-01", date_embout_iso="2026-01-15", id_produit=1,
        )
        assert p["pourcentagePerte"] == 5.0

    def test_percentage_zero_volume(self):
        p = build_brassin_payload(
            code="T", vol_l=0.0, perte_litres=10.0,
            semaine_du="2026-01-01", date_embout_iso="2026-01-15", id_produit=1,
        )
        assert p["pourcentagePerte"] == 0

    def test_date_formatting(self):
        p = build_brassin_payload(
            code="T", vol_l=100.0, perte_litres=0.0,
            semaine_du="2026-03-10", date_embout_iso="2026-03-24", id_produit=1,
        )
        assert p["dateDebutFormulaire"] == "2026-03-10T07:30:00.000Z"
        assert p["dateConditionnementPrevue"] == "2026-03-24T23:00:00.000Z"

    def test_ingredients_included_when_provided(self):
        ing = [{"idProduitIngredient": 1, "quantite": 10}]
        p = build_brassin_payload(
            code="T", vol_l=100.0, perte_litres=0.0,
            semaine_du="2026-01-01", date_embout_iso="2026-01-15",
            id_produit=1, ingredients=ing,
        )
        assert p["ingredients"] == ing

    def test_ingredients_omitted_when_none(self):
        p = build_brassin_payload(
            code="T", vol_l=100.0, perte_litres=0.0,
            semaine_du="2026-01-01", date_embout_iso="2026-01-15", id_produit=1,
        )
        assert "ingredients" not in p

    def test_ingredients_omitted_when_empty_list(self):
        p = build_brassin_payload(
            code="T", vol_l=100.0, perte_litres=0.0,
            semaine_du="2026-01-01", date_embout_iso="2026-01-15",
            id_produit=1, ingredients=[],
        )
        assert "ingredients" not in p

    def test_planif_etapes_included_when_provided(self):
        etapes = [{"idProduitEtape": 1}]
        p = build_brassin_payload(
            code="T", vol_l=100.0, perte_litres=0.0,
            semaine_du="2026-01-01", date_embout_iso="2026-01-15",
            id_produit=1, planif_etapes=etapes,
        )
        assert p["planificationsEtapes"] == etapes

    def test_planif_etapes_omitted_when_none(self):
        p = build_brassin_payload(
            code="T", vol_l=100.0, perte_litres=0.0,
            semaine_du="2026-01-01", date_embout_iso="2026-01-15", id_produit=1,
        )
        assert "planificationsEtapes" not in p


# ─── _norm_etape ──────────────────────────────────────────────────────────────


class TestNormEtape:

    def test_accent_removal(self):
        assert _norm_etape("Fermentation") == "fermentation"

    def test_accent_removal_french(self):
        assert _norm_etape("Préparation") == "preparation"

    def test_case_normalization(self):
        assert _norm_etape("TRANSFERT") == "transfert"

    def test_empty_string(self):
        assert _norm_etape("") == ""

    def test_combined_accents(self):
        assert _norm_etape("Étape spéciale") == "etape speciale"


# ─── scale_recipe_ingredients ─────────────────────────────────────────────────


class TestScaleRecipeIngredients:

    def test_ratio_scaling_double(self):
        recette = {
            "volumeRecette": 1000,
            "ingredients": [
                {"idProduitIngredient": 1, "quantite": 50.0, "ordre": 1, "unite": {"symbole": "kg"},
                 "matierePremiere": {"id": 10}, "brassageEtape": {"id": 1}},
            ],
        }
        result = scale_recipe_ingredients(recette, 2000.0)
        assert len(result) == 1
        assert result[0]["quantite"] == 100.0

    def test_ratio_scaling_half(self):
        recette = {
            "volumeRecette": 1000,
            "ingredients": [
                {"idProduitIngredient": 1, "quantite": 80.0, "ordre": 1, "unite": None,
                 "matierePremiere": None, "brassageEtape": None},
            ],
        }
        result = scale_recipe_ingredients(recette, 500.0)
        assert result[0]["quantite"] == 40.0

    def test_empty_recipe_ingredients(self):
        recette = {"volumeRecette": 1000, "ingredients": []}
        result = scale_recipe_ingredients(recette, 2000.0)
        assert result == []

    def test_missing_ingredients_key(self):
        recette = {"volumeRecette": 1000}
        result = scale_recipe_ingredients(recette, 2000.0)
        assert result == []

    def test_zero_volume_recette_uses_ratio_1(self):
        recette = {
            "volumeRecette": 0,
            "ingredients": [
                {"idProduitIngredient": 1, "quantite": 25.0, "ordre": 1, "unite": None,
                 "matierePremiere": None, "brassageEtape": None},
            ],
        }
        result = scale_recipe_ingredients(recette, 5000.0)
        # ratio = 1 when volumeRecette == 0
        assert result[0]["quantite"] == 25.0

    def test_preserves_all_fields(self):
        ing = {
            "idProduitIngredient": 99,
            "matierePremiere": {"id": 5, "libelle": "Sucre"},
            "quantite": 10.0,
            "ordre": 3,
            "unite": {"symbole": "kg"},
            "brassageEtape": {"id": 2, "nom": "Fermentation"},
        }
        recette = {"volumeRecette": 100, "ingredients": [ing]}
        result = scale_recipe_ingredients(recette, 100.0)  # ratio = 1
        r = result[0]
        assert r["idProduitIngredient"] == 99
        assert r["matierePremiere"] == {"id": 5, "libelle": "Sucre"}
        assert r["ordre"] == 3
        assert r["unite"] == {"symbole": "kg"}
        assert r["brassageEtape"] == {"id": 2, "nom": "Fermentation"}
        assert r["modeleNumerosLots"] == []


# ─── build_etape_planification ────────────────────────────────────────────────


class TestBuildEtapePlanification:

    def _make_etape(self, nom: str, *, id_etape: int = 1, ordre: int = 1) -> dict:
        return {
            "idProduitEtape": id_etape,
            "brassageEtape": {"nom": nom},
            "ordre": ordre,
            "duree": 48,
            "unite": {"symbole": "h"},
        }

    def test_fermentation_gets_cuve_a(self):
        result = build_etape_planification([self._make_etape("Fermentation")], cuve_a_id=10)
        assert result[0]["materiel"] == {"idMateriel": 10}

    def test_aromatisation_gets_cuve_a(self):
        result = build_etape_planification([self._make_etape("Aromatisation")], cuve_a_id=10)
        assert result[0]["materiel"] == {"idMateriel": 10}

    def test_filtration_gets_cuve_a(self):
        result = build_etape_planification([self._make_etape("Filtration")], cuve_a_id=10)
        assert result[0]["materiel"] == {"idMateriel": 10}

    def test_transfert_gets_cuve_b(self):
        result = build_etape_planification([self._make_etape("Transfert")], cuve_b_id=20)
        assert result[0]["materiel"] == {"idMateriel": 20}

    def test_garde_gets_cuve_b(self):
        result = build_etape_planification([self._make_etape("Garde")], cuve_b_id=20)
        assert result[0]["materiel"] == {"idMateriel": 20}

    def test_preparation_gets_cuve_dilution(self):
        result = build_etape_planification([self._make_etape("Préparation sirop")], cuve_dilution_id=30)
        assert result[0]["materiel"] == {"idMateriel": 30}

    def test_sirop_gets_cuve_dilution(self):
        result = build_etape_planification([self._make_etape("Sirop")], cuve_dilution_id=30)
        assert result[0]["materiel"] == {"idMateriel": 30}

    def test_no_cuve_when_ids_none(self):
        result = build_etape_planification([self._make_etape("Fermentation")])
        assert result[0]["materiel"] == {}

    def test_unmatched_step_gets_empty_materiel(self):
        result = build_etape_planification(
            [self._make_etape("Embouteillage")], cuve_a_id=10, cuve_b_id=20, cuve_dilution_id=30,
        )
        assert result[0]["materiel"] == {}

    def test_produit_etape_structure(self):
        et = self._make_etape("Fermentation", id_etape=5, ordre=2)
        result = build_etape_planification([et], cuve_a_id=10)
        pe = result[0]["produitEtape"]
        assert pe["idProduitEtape"] == 5
        assert pe["ordre"] == 2
        assert pe["duree"] == 48
        assert pe["etapeTerminee"] is False
        assert pe["etapeEnCours"] is False


# ─── parse_packaging_lookup ───────────────────────────────────────────────────


class TestParsePackagingLookup:

    def test_normal_extraction(self):
        matrice = {
            "packagings": [
                {"libelle": "Carton 12x33cl", "idLot": 100},
                {"libelle": "Pack 6x75cl", "idLot": 200},
            ],
        }
        lookup = parse_packaging_lookup(matrice)
        assert lookup == {"carton 12x33cl": 100, "pack 6x75cl": 200}

    def test_empty_libelle_skipped(self):
        matrice = {"packagings": [{"libelle": "", "idLot": 100}, {"libelle": "  ", "idLot": 200}]}
        lookup = parse_packaging_lookup(matrice)
        assert lookup == {}

    def test_missing_libelle_skipped(self):
        matrice = {"packagings": [{"idLot": 100}]}
        lookup = parse_packaging_lookup(matrice)
        assert lookup == {}

    def test_missing_id_lot_skipped(self):
        matrice = {"packagings": [{"libelle": "Carton", "idLot": None}]}
        lookup = parse_packaging_lookup(matrice)
        assert lookup == {}

    def test_empty_matrice(self):
        assert parse_packaging_lookup({}) == {}
        assert parse_packaging_lookup({"packagings": []}) == {}


# ─── parse_derive_map ─────────────────────────────────────────────────────────


class TestParseDeriveMap:

    def test_niko_inter_water_detected(self):
        matrice = {
            "produitsDerives": [
                {"libelle": "Niko Citron 33cl", "idProduit": 1},
                {"libelle": "Inter Mangue 75cl", "idProduit": 2},
                {"libelle": "Water Kéfir 1L", "idProduit": 3},
            ],
        }
        derive = parse_derive_map(matrice)
        assert derive == {"niko": 1, "inter": 2, "water": 3}

    def test_unknown_keyword_ignored(self):
        matrice = {"produitsDerives": [{"libelle": "Bouteille standard", "idProduit": 10}]}
        derive = parse_derive_map(matrice)
        assert derive == {}

    def test_missing_id_produit_skipped(self):
        matrice = {"produitsDerives": [{"libelle": "Niko Citron", "idProduit": None}]}
        derive = parse_derive_map(matrice)
        assert derive == {}

    def test_empty_matrice(self):
        assert parse_derive_map({}) == {}
        assert parse_derive_map({"produitsDerives": []}) == {}


# ─── match_contenant_id ───────────────────────────────────────────────────────


class TestMatchContenantId:

    def test_single_candidate_returns_id(self):
        by_vol = {0.33: [{"idContenant": 50, "libelleAvecContenance": "Bouteille 33cl"}]}
        assert match_contenant_id("Carton de 12", 0.33, by_vol) == 50

    def test_multi_candidates_pack_selects_saft(self):
        by_vol = {
            0.33: [
                {"idContenant": 50, "libelleAvecContenance": "Bouteille 33cl"},
                {"idContenant": 60, "libelleAvecContenance": "Bouteille 33cl SAFT"},
            ],
        }
        result = match_contenant_id("Pack de 6 bouteilles", 0.33, by_vol)
        assert result == 60

    def test_multi_candidates_non_pack_selects_non_saft(self):
        by_vol = {
            0.33: [
                {"idContenant": 50, "libelleAvecContenance": "Bouteille 33cl"},
                {"idContenant": 60, "libelleAvecContenance": "Bouteille 33cl SAFT"},
            ],
        }
        result = match_contenant_id("Carton de 12", 0.33, by_vol)
        assert result == 50

    def test_none_vol_returns_none(self):
        by_vol = {0.33: [{"idContenant": 50}]}
        assert match_contenant_id("Carton de 12", None, by_vol) is None

    def test_nan_vol_returns_none(self):
        by_vol = {0.33: [{"idContenant": 50}]}
        assert match_contenant_id("Carton de 12", math.nan, by_vol) is None

    def test_volume_not_in_dict_returns_none(self):
        by_vol = {0.75: [{"idContenant": 50}]}
        assert match_contenant_id("Carton de 12", 0.33, by_vol) is None

    def test_fallback_to_first_candidate(self):
        """When no saft/non-saft match is decisive, return the first candidate."""
        by_vol = {
            0.33: [
                {"idContenant": 70, "libelleAvecContenance": "Type A saft"},
                {"idContenant": 80, "libelleAvecContenance": "Type B saft"},
            ],
        }
        # "carton de 12" is not a pack, so we look for non-saft — but both are saft
        result = match_contenant_id("Carton de 12", 0.33, by_vol)
        assert result == 70
