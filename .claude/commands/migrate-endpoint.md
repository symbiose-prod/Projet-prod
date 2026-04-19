---
description: Migre un endpoint EasyBeer vers le helper execute_endpoint
argument-hint: <nom ou chemin de l'endpoint, ex: "get_brassin_detail" ou "common/easybeer/stocks.py:get_autonomie_stocks">
---

# /project:migrate-endpoint — Migrer un endpoint EasyBeer vers `execute_endpoint`

Consolide le boilerplate HTTP (auth + circuit breaker + logging + cache L2 + parsing défensif) via le descriptor déclaratif [common/easybeer/endpoint.py](../../common/easybeer/endpoint.py).

## Contexte projet

- Couche transport : `common/easybeer/*.py` contient les endpoints par tag
- Pattern établi : `execute_endpoint(method, path, ...)` remplace ~25-40 LOC de boilerplate par 5-12 LOC déclaratives
- Voir `common/easybeer/products.py` ou `common/easybeer/stocks.py` pour des exemples de référence
- Documentation : [docs/ARCHITECTURE.md § "Pattern : ajouter un endpoint EasyBeer"](../../docs/ARCHITECTURE.md)

## Procédure

1. **Identifier l'endpoint** à migrer (argument `$ARGUMENTS`). Lire le code actuel pour capturer :
   - méthode HTTP (GET/POST), path sans `{BASE}/`
   - params query (surtout `forceRefresh=false` pour `/indicateur/*`)
   - payload JSON (pour les POST — vérifier `_indicator_payload` / gotcha `PERIODE_LIBRE`)
   - stratégie de cache actuelle (L1 in-memory ? L2 DB ? pas de cache ?)
   - présence de `@retry_api`

2. **Décider de la stratégie de migration** :
   - Cache raw → utiliser `cache_key` + `cache_item_id` + `cache_ttl` dans `execute_endpoint`
   - Cache processé (liste filtrée/triée avant persist) → garder cache local, n'utiliser `execute_endpoint` que pour le HTTP (pas de `cache_key`)
   - Retour binaire (Excel, PDF) → **ne pas migrer** (hors scope)
   - Multipart upload → **ne pas migrer** (hors scope)

3. **Réécrire l'endpoint** :
   ```python
   from .endpoint import execute_endpoint

   @retry_api  # si idempotent
   def get_xxx(arg: int) -> dict[str, Any]:
       return execute_endpoint(
           method="GET",
           path=f"ressource/{arg}",
           cache_key="xxx",                  # optionnel
           cache_item_id=str(arg),           # si cache_key
           cache_ttl=1800,                   # secondes
           response_model=XxxModel,          # si dataclass typée disponible
       )
   ```

4. **Si L1 in-memory présent**, conserver le wrapper :
   ```python
   @retry_api
   def get_xxx() -> list[dict[str, Any]]:
       # L1 check (lock thread-safe)
       with _cache_lock:
           if _cache_valid(_xxx_cache):
               return _xxx_cache["data"]
       # L2 + L3 via helper
       data = execute_endpoint(method="GET", path="…", cache_key="xxx")
       result = data if isinstance(data, list) else []
       if result:
           with _cache_lock:
               _xxx_cache["data"] = result
               _xxx_cache["ts"] = _time.monotonic()
       return result
   ```

5. **Nettoyer les imports** — retirer `BASE`, `TIMEOUT`, `_auth`, `_check_response`, `_safe_json`, `get_session` s'ils ne sont plus utilisés dans le fichier. Garder `_log`, `retry_api`, et les helpers spécifiques (`is_rate_limited`, `_safe_list`…).

6. **Vérifier** :
   - `python3 -m pytest tests/ -q` — tous les tests doivent passer
   - `python3 -m ruff check common/easybeer/` — pas d'erreur
   - Les 4 guards d'architecture tournent dans la suite — vérifier qu'ils restent verts

7. **Commit** au format `refactor(easybeer): migrer <fonction> vers execute_endpoint` avec mention du pattern utilisé (bare GET / POST body / cache L2 / response_model).

## Erreurs courantes à éviter

- Oublier `params={"forceRefresh": False}` sur les endpoints `/indicateur/*` JSON → HTTP 500
- Oublier `"type": "PERIODE_LIBRE"` dans les payloads `periode` → HTTP 500
- Supprimer `@retry_api` sur une fonction non-idempotente (ex: `create_brassin`) → retries involontaires sur POST
- Utiliser `cache_key` alors que le caller veut cacher un résultat processé — on finirait par cacher le raw + le processé, gaspillage

## Argument reçu

Tu dois migrer : `$ARGUMENTS`
