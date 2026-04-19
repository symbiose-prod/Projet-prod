---
description: Ajoute une dataclass typée pour une réponse EasyBeer avec parsing défensif
argument-hint: <nom du modèle + référence schéma, ex: "Recipe (ModeleRecette)" ou "BrassinDetail (GET /brassin/{id})">
---

# /project:add-typed-model — Ajouter une dataclass typée dans `common/easybeer/models.py`

Étend la base de typage stdlib pour une nouvelle entité EasyBeer. Pattern : `@dataclass(frozen=True)` + `from_dict()` défensif + optionnellement un champ `raw: dict` pour compat legacy + properties métier.

## Contexte projet

- Fichier cible : [common/easybeer/models.py](../../common/easybeer/models.py)
- Pas de pydantic — stdlib uniquement, helpers `_as_str`, `_as_int`, `_as_float` fournis
- Modèles existants : `AutonomieProduit`, `AutonomieResponse`, `MatierePremiere`, `StockProduitFormat`, `BrassinLight`, `FournisseurContact`, `Fournisseur`
- Tests : `tests/test_easybeer_models.py` — un test par modèle couvrant happy path / null / missing / wrong-type / input non-dict
- Consommateurs typiques : `execute_endpoint(..., response_model=MonModele)` parse directement

## Procédure

1. **Spec le modèle** à partir de `docs/easybeer/*.json` ou d'un exemple de réponse API. Identifier :
   - Nom EasyBeer du modèle (`ModeleXxx`)
   - Champs utilisés côté Ferment Station (NE PAS mapper tout — on ajoute au besoin)
   - Champs optionnels qui peuvent être `null` côté API
   - Sous-objets imbriqués (créer une dataclass séparée si partagée, inline si usage unique)

2. **Écrire la dataclass** avec ce squelette :
   ```python
   @dataclass(frozen=True)
   class MonModele:
       """Schema EasyBeer: ``ModeleXxx``.

       <Description courte du contexte d'usage + références aux callers principaux>
       """
       id_xxx: int
       libelle: str
       # ... champs nécessaires uniquement

       # Optionnel : conservation du payload brut pour consommateurs non-migrés.
       # Même pattern que BrassinLight / Fournisseur — à documenter ou retirer
       # quand plus de callers legacy.
       raw: dict = field(default_factory=dict)

       @classmethod
       def from_dict(cls, d: dict[str, Any]) -> MonModele:
           if not isinstance(d, dict):
               return cls(0, "", {})   # defaults sûrs — PAS de raise
           return cls(
               id_xxx=_as_int(d.get("idXxx")),
               libelle=_as_str(d.get("libelle")),
               raw=d,
           )
   ```

3. **Règles défensives obligatoires** (`from_dict` ne doit jamais crasher) :
   - `isinstance(d, dict)` en garde d'entrée
   - `_as_str(d.get("key"))` — jamais `d["key"]`
   - `_as_int(d.get("idX"))` pour les IDs (EasyBeer peut renvoyer null ou string)
   - `_as_float(d.get("volume"))` pour les nombres
   - Sous-objets : `sub = d.get("sub") if isinstance(d.get("sub"), dict) else {}`
   - Listes : `raw_list = d.get("items") or []` puis `isinstance(raw_list, list)`

4. **Properties métier** — si la logique d'accès est non-triviale (priorités, fallbacks, formatage), l'encapsuler en `@property` plutôt que laisser le caller dupliquer. Exemples existants :
   - `Fournisseur.best_email` — priorité `contactPrincipal > contact > contacts[0]`
   - `Fournisseur.full_address_lines` — construction multi-lignes avec fallback
   - `FournisseurContact.display_name` — concat prénom + nom

5. **Exporter depuis `common/easybeer/__init__.py`** :
   ```python
   from .models import ..., MonModele
   ```
   Et ajouter au `__all__`.

6. **Écrire les tests** dans `tests/test_easybeer_models.py` :
   - `test_full_dict` — happy path complet
   - `test_missing_fields_default_to_zero` — defaults sûrs
   - `test_null_fields_default_to_zero` — résistance aux `null`
   - `test_wrong_types_default_safely` — coercition défensive
   - `test_non_dict_input` — `from_dict(None)` ne crash pas
   - Si sous-objets : `test_missing_nested_objects`, `test_null_nested_objects`
   - Si properties : un test par property (priorités, fallbacks)

7. **Si le modèle est utilisé avec `execute_endpoint(response_model=…)`**, vérifier que :
   - `cls()` sans arguments produit une instance sûre (utilisé comme fallback sur payload non-dict)
   - Les tests d'intégration `test_easybeer_endpoint.py` couvrent le pipeline complet

8. **Vérifier** :
   - `python3 -m pytest tests/test_easybeer_models.py -v`
   - `python3 -m ruff check common/easybeer/ tests/`

9. **Commit** au format `feat(easybeer): modèle typé MonModele + tests`.

## Erreurs courantes

- Oublier `field(default_factory=dict)` pour `raw` → TypeError sur instanciation
- Utiliser `d["key"]` au lieu de `d.get("key")` dans `from_dict` → crash si clé absente
- Mapper TOUS les champs EasyBeer dès le départ → bruit, préférer ajouter au besoin
- Faire des dataclasses non-frozen → mutations inattendues, casse l'invariant immuable

## Argument reçu

Modèle à ajouter : `$ARGUMENTS`
