---
description: Extrait une logique métier d'une page NiceGUI vers la couche services
argument-hint: <path/to/page.py:fonction_ou_bloc> (ex: "pages/ramasse.py:build_email_body")
---

# /project:extract-service — Extraire de la logique vers `common/services/`

Sépare la logique métier pure de l'UI. Objectifs : testabilité sans NiceGUI, composabilité inter-pages, réduction des god files.

## Contexte projet

- 3 couches : transport (`common/easybeer/`) → domaine (`common/services/`) → UI (`pages/`)
- Règles dures (vérifiées par `tests/test_architecture_layers.py`) :
  - `common/services/` n'importe **pas** `nicegui`
  - `common/services/` n'importe **pas** `pages/`
  - `pages/X` n'importe **pas** `pages/Y` sauf `auth`, `theme`, ou `_xxx` privés
- Services existants : `stocks_service.py`, `ramasse_service.py`, `production_service.py`
- Documentation : [docs/ARCHITECTURE.md § "Pattern : ajouter un service"](../../docs/ARCHITECTURE.md)

## Procédure

1. **Identifier le code à extraire** (argument `$ARGUMENTS`). Critères de bon candidat :
   - Fonction ou bloc dans `pages/X.py`
   - Ne touche pas à `ui.*`, `app.storage`, `app.add_head_html`, etc.
   - Reçoit des primitifs ou dataclasses, retourne des primitifs ou dataclasses
   - Pas d'accès async UI (les `await asyncio.to_thread(...)` côté page peuvent entourer le service)

2. **Choisir le service cible** :
   - Si `common/services/<domain>_service.py` existe → ajouter dedans
   - Sinon → créer un nouveau fichier avec le header standard (voir template plus bas)

3. **Déplacer le code** en suivant ces règles :
   - Typer les signatures (éviter `dict[str, Any]` quand une dataclass existe)
   - Sortir typé : retour sous forme de `@dataclass(frozen=True)` si structuré
   - Docstring explicite (flow + invariants)
   - Logger dédié : `_log = logging.getLogger("ferment.services.<domain>")`
   - Aucun `from nicegui import …` ni `from pages.<x> import …` (sauf `TYPE_CHECKING`)

4. **Template d'un nouveau service** :
   ```python
   """
   common/services/<domain>_service.py
   ===================================
   Service domaine : <description courte>.
   """
   from __future__ import annotations
   import logging
   from dataclasses import dataclass
   # imports depuis common/easybeer, common/*, db/* uniquement

   _log = logging.getLogger("ferment.services.<domain>")


   @dataclass(frozen=True)
   class <Domain>Result:
       """Résultat typé retourné par le service."""
       ...


   def do_something(arg: str) -> <Domain>Result:
       """Docstring avec flow métier."""
       ...
   ```

5. **Mettre à jour les callers** dans `pages/` :
   - Remplacer l'appel inline par `from common.services.<domain>_service import do_something`
   - Si la fonction était bloquante, maintenir le wrapping `await asyncio.to_thread(do_something, ...)` côté page

6. **Écrire des tests unitaires** dans `tests/test_services_<domain>.py` :
   - Mocks légers (pas de setup NiceGUI, pas de DB réelle)
   - Couvrir : happy path, valeurs nulles / manquantes, cas limites métier
   - Pattern : `@patch("common.services.<domain>_service.<dep>")` pour mocker les dépendances

7. **Vérifier** :
   - `python3 -m pytest tests/ -q` — tests verts (+ ceux ajoutés)
   - `python3 -m pytest tests/test_architecture_layers.py -v` — 4 guards passent (pas de régression)
   - `python3 -m ruff check common/ pages/ tests/`

8. **Commit** au format `refactor(services): extraire <fonction> vers common/services/<domain>_service` avec explicitation du gain (LOC retirées de la page, tests ajoutés, composition permise).

## Erreurs courantes

- Importer `nicegui.ui` dans le service (même indirectement via un helper) → guard CI rouge
- Oublier de mettre à jour `CLAUDE.md` ou `ARCHITECTURE.md` si le service introduit une nouvelle règle
- Refactorer agressivement en même temps que l'extraction — préférer "move then improve" en 2 commits

## Argument reçu

Tu dois extraire : `$ARGUMENTS`
