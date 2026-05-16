# Architecture — Ferment Station

**Statut :** Vivant. Mis à jour 2026-05-16 après l'extraction de la couche
mobile API (`common/mobile_v1.py`) et l'unification du pipeline de
génération d'étiquettes (`generate_and_save_palette_label`).
**Public cible :** Développeur qui ajoute une feature et veut savoir où placer son code sans casser l'équilibre du projet.

---

## TL;DR — Les 3 couches + l'API mobile

```
┌──────────────────────────────────┐    ┌─────────────────────────────────┐
│  pages/        ← UI (NiceGUI)   │    │  common/mobile_v1.py            │
│  pages/theme.py, auth.py, …      │    │  ← UI/transport (FastAPI routes)│
│                                  │    │  /api/v1/* pour l'app iOS       │
└──────────────────┬───────────────┘    └────────────────┬────────────────┘
                   │                                     │
                   └────────────────┬────────────────────┘
                                    │  appellent les MÊMES services
                                    ▼
              ┌─────────────────────────────────────────────────┐
              │  common/services/                ← DOMAINE      │  Logique métier pure
              │  stocks_service, ramasse_service, production_*, │
              │  etiquette_palette_service, sscc_service…       │
              │  → `generate_and_save_palette_label` est l'EX-  │
              │    EMPLE de fonction partagée web + mobile      │
              └────────────┬─────────────────┬──────────────────┘
                           │ orchestre       │ persiste
                           ▼                 ▼
              ┌──────────────────────┐ ┌─────────────────────────┐
              │ common/easybeer/     │ │ db/conn.py + common/*  │ ← TRANSPORT / INFRA
              │ (HTTP + cache +      │ │  (SQL, audit, auth,    │
              │  circuit breaker)    │ │   email, storage,      │
              │                      │ │   mobile_auth)         │
              └──────────────────────┘ └─────────────────────────┘
```

**Règles** :
- Le sens des flèches compte : une couche basse ne peut jamais importer d'une couche haute.
- `pages/` (web) et `common/mobile_v1.py` (API mobile) sont **2 transports
  équivalents** — ils ne contiennent QUE du parsing input + appel service +
  formatage output. Toute logique métier vit dans `common/services/`.
- Une fonction du service doit pouvoir être appelée par les 2 sans dépendre
  de NiceGUI ni de FastAPI.

---

## Règles dures (enforced par CI)

### Interdictions

| Règle | Rationale |
|-------|-----------|
| `common/` ne peut **pas** importer depuis `pages/` | Services et infra doivent rester utilisables sans NiceGUI (CLI, cron, tests unitaires) |
| `common/services/` ne peut **pas** importer `nicegui` | Les services doivent tourner dans un script Python pur |
| `common/easybeer/` ne peut **pas** importer `common/services/` | Le transport ignore la logique métier — symétrique inversé |
| `pages/X.py` ne peut **pas** importer `pages/Y.py` sauf `pages/auth`, `pages/theme` | Évite l'emmêlement entre pages. Si partage nécessaire → extraire un service |

Ces règles sont vérifiées par `scripts/check_layers.py` (lancé en CI).

### Obligations

- Tout nouvel endpoint EasyBeer doit être ajouté dans `common/easybeer/*.py`, **pas directement dans une page**.
- Tout appel HTTP externe passe par `common/easybeer/_client.py` (retry, throttle, circuit breaker automatiques).
- Toute réponse API destinée à être consommée par >1 caller doit avoir un modèle typé dans `common/easybeer/models.py`.
- Toute opération DB écrivant dans `ramasse_history`, `production_proposals`, `audit_log` passe par un module dédié dans `common/` (pas de SQL inline dans les pages).

---

## Que mettre où ? — Décisions rapides

| Tu ajoutes... | Ça va dans... | Exemple existant |
|---------------|---------------|------------------|
| Un appel HTTP EasyBeer | `common/easybeer/<tag>.py` | `common/easybeer/brassins.py` |
| Un modèle typé d'une réponse API | `common/easybeer/models.py` | `AutonomieProduit` |
| Une fonction métier qui orchestre plusieurs appels | `common/services/<domaine>_service.py` | `stocks_service.fetch_and_compute_bom` |
| Une page ou un composant UI | `pages/` | `pages/chargement_camion.py` |
| Un composant UI réutilisable | `pages/theme.py` | `confirm_dialog`, `error_banner` |
| Une requête SQL | Module dédié dans `common/` | `common/ramasse_history.py` |
| Un audit trail | Via `common/audit.log_event` | Voir `common/ramasse_history._audit()` |
| Une variable d'env | `.env` + accès via `os.environ.get` avec default | — |
| Une constante métier (délais, seuils) | `config.yaml` + `common/data.py` | `DEFAULT_LOSS_LARGE` |

---

## Pattern : ajouter un endpoint EasyBeer

**Recommandé** : utiliser le helper déclaratif `execute_endpoint`
([common/easybeer/endpoint.py](../common/easybeer/endpoint.py)) qui consolide
tout le boilerplate (auth, circuit breaker, logging, cache L2 DB, parsing
typé défensif).

### Endpoint sans cache, sans modèle (le plus simple)

```python
from common.easybeer.endpoint import execute_endpoint

@retry_api
def get_warehouse_detail(id_entrepot: int) -> dict:
    return execute_endpoint(
        method="GET",
        path=f"parametres/entrepot/{id_entrepot}",
    )
```

### Avec cache L2 DB (partagé entre processus)

```python
@retry_api
def get_autonomie_stocks(window_days: int) -> dict:
    return execute_endpoint(
        method="POST",
        path="indicateur/autonomie-stocks",
        params={"forceRefresh": False},               # gotcha EB
        payload=_indicator_payload(window_days),       # gotcha PERIODE_LIBRE
        cache_key="autonomie_stocks",
        cache_item_id=str(window_days),
        cache_ttl=1800,                                # 30 min
    )
```

### Avec modèle typé (IDE autocomplete + parsing défensif)

```python
@retry_api
def get_autonomie_stocks_typed(window_days: int) -> AutonomieResponse:
    return execute_endpoint(
        method="POST",
        path="indicateur/autonomie-stocks",
        params={"forceRefresh": False},
        payload=_indicator_payload(window_days),
        cache_key="autonomie_stocks",
        cache_item_id=str(window_days),
        response_model=AutonomieResponse,  # ← parsé automatiquement
    )
```

### Ce que `execute_endpoint` ne gère PAS (volontairement)

- **Cache L1 in-memory** : spécifique par endpoint avec logique d'invalidation
  métier. Voir `common/easybeer/products.py` pour les patterns dédiés.
- **Désérialisation binaire** (Excel, PDF) : pour les endpoints qui retournent
  `bytes` (ex: `/export/excel`), continuer à appeler `get_session()` directement.
- **`@retry_api`** : volontairement hors du descriptor — certains endpoints
  (ex: `POST /brassin/enregistrer`) ne doivent pas être retry automatiquement
  (non-idempotents). Toujours décorer explicitement côté caller.

### Checklist "nouvel endpoint"

### Checklist "nouvel endpoint"

- [ ] Utiliser `execute_endpoint` (pas de boilerplate à réécrire)
- [ ] Méthode + path dans le module thématique approprié (`brassins.py`, `stocks.py`, …)
- [ ] Payload utilise `_indicator_payload(window_days)` si applicable (gotcha `PERIODE_LIBRE`)
- [ ] Param `?forceRefresh=false` si endpoint `/indicateur/*` JSON (pas `/export/excel`)
- [ ] `@retry_api` sur la fonction publique si l'endpoint est idempotent
- [ ] `cache_key` + `cache_item_id` si >1 caller ou appel fréquent
- [ ] `response_model` si une dataclass typée existe (sinon `dict[str, Any]` accepté)
- [ ] Si la réponse est itérable et que le descriptor n'est pas utilisé →
      `_safe_list(data, "key", ep)` (évite crash sur `null`)
- [ ] Export dans `common/easybeer/__init__.py`
- [ ] Modèle typé dans `models.py` si >1 caller prévu

---

## Pattern : ajouter un service

Quand une logique métier orchestre plusieurs endpoints ou contient des transformations qu'on voudrait tester sans NiceGUI.

**Structure d'un service :**

```python
# common/services/my_service.py
"""
common/services/my_service.py
=============================
Service domaine : <description courte>.
"""
from __future__ import annotations
from dataclasses import dataclass
# imports depuis common/easybeer, common/*, db/*  — JAMAIS depuis pages/

_log = logging.getLogger("ferment.services.my")


@dataclass(frozen=True)
class MyServiceResult:
    """Résultat typé retourné par le service."""
    ...


def do_something(arg: str) -> MyServiceResult:
    """Docstring avec flow métier."""
    ...
```

### Checklist "nouveau service"

- [ ] Ni `nicegui`, ni `pages.` dans les imports
- [ ] Retour sous forme de `@dataclass(frozen=True)` (pas de `dict[str, Any]`)
- [ ] Docstring explique le flow (enchaînement des appels, invariants)
- [ ] Tests unitaires dans `tests/test_services_<domaine>.py` avec mocks légers (pas de DB réelle)
- [ ] Appelé depuis la page via `asyncio.to_thread(service_fn)` si bloquant

---

## Pattern : ajouter une page

```python
# pages/my_page.py
from nicegui import ui
from common.services.my_service import do_something   # couche domaine
from pages.auth import require_auth
from pages.theme import page_layout, section_title


@ui.page("/my-page")
def page_my_page():
    user = require_auth()
    if not user:
        return

    with page_layout("Titre page", "icon_name", "/my-page"):
        section_title("Section", "icon")
        # UI + appel do_something(...)
```

Puis enregistrer dans `app_nicegui.py` :

```python
import pages.my_page  # noqa: F401 — /my-page
```

---

## Multi-tenancy — invariants

- Chaque ligne de chaque table métier porte un `tenant_id` (FK vers `tenants`).
- Toutes les queries DB incluent `WHERE tenant_id = :tid` OU utilisent `run_sql_with_tenant(..., tenant_id=...)` qui positionne la variable session et laisse la **RLS Postgres** filtrer.
- Le `tenant_id` est obtenu via `common._session.current_tenant_id()` — ne **jamais** lire `app.storage.user["tenant_id"]` directement dans un service (couplage NiceGUI).
- L'`AuthMiddleware` refuse toute session authentifiée sans `tenant_id` valide et expose `request.state.tenant_id` pour les routes FastAPI (ex: `/api/sync`).

## Fiabilité — les garde-fous actifs

| Mécanisme | Fichier | Déclencheur |
|-----------|---------|-------------|
| Circuit breaker EasyBeer | `common/easybeer/_client.py` | 5× 5xx consécutifs → open 60s |
| Rate limit EasyBeer (sortant) | `common/easybeer/_client.py` | Throttle 1 req/s (sous limite EB à 10) |
| Rate limit `/api/sync` (entrant) | `common/sync/rate_limit.py` | 60 req/min par clé API → 429 |
| Fallback Brevo → queue DB | `common/email_queue.py` | Échec immédiat → enqueue + retry cron (10 min) |
| Soft-delete ramasses | `common/ramasse_history.py` | 7 j de récupération + purge cron |
| Audit log fire-and-forget | `common/audit.py` | 2 tentatives DB + fallback logger |

---

## Observabilité

- **Logs structurés** : format JSON en `ENV=production` (`app_nicegui.py` boot). Logger par module (`ferment.services.ramasse`, `ferment.easybeer`, etc.).
- **`/health`** : `db` + `disk` + état EasyBeer (circuit, rate-limit) + taille cache DB.
- **`/metrics`** : format Prometheus texte (gauges) — scrapable par Grafana/Alertmanager.
- **Audit log** : persisté en DB + exporté via page `/admin` (role=admin).

---

## Conventions de code

- **Type hints partout** sur les signatures publiques. `dict[str, Any]` toléré uniquement pour les réponses EasyBeer pas encore typées.
- **`@dataclass(frozen=True)`** par défaut pour les modèles — immuabilité = pas de mutation surprise.
- **Pas de magic numbers** — les constantes métier vont dans `config.yaml`.
- **Commits thématiques** : `feat(ramasse): ...`, `fix(easybeer): ...`, `refactor(services): ...`, `security(db): ...`, `ops(email): ...`, `docs(...): ...`.

---

## Points d'évolution connus

- [ ] `pages/chargement_camion.py` (~1700 LOC), `pages/stocks.py` (1527 LOC), `pages/production.py` (1129 LOC) restent denses. Extraction progressive via services à chaque PR qui les touche.
- [ ] `_render_easybeer_section` dans `pages/_production_easybeer.py` mélange encore UI et logique — candidat pour un futur `production_brassins_service`.
- [ ] RLS Postgres est en mode permissif (compatible owner `shark`). Full-enforcement nécessite un rôle applicatif dédié non-owner — étape planifiée.
- [ ] Migration vers Pydantic / modèles typés en cours (voir `common/easybeer/models.py`). Certaines fonctions retournent encore `dict[str, Any]`.
- [ ] Pas encore d'event bus — les invalidations de cache et les appels audit sont impératifs. Sera pertinent si >3 listeners apparaissent pour un même événement.
