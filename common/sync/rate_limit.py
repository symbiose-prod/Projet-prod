"""
common/sync/rate_limit.py
=========================
Sliding-window rate limiter pour l'API /api/sync.

Protège contre les bursts d'un agent Windows mal configuré (boucle infinie,
retry-storm) qui surchargerait la DB. Limite per-key : chaque clé API a son
propre quota indépendant.

Design :
- Stockage in-memory (deque de timestamps par key_id), thread-safe (lock).
- Sliding window : on garde les N dernières secondes d'activité, on refuse
  si le nombre dépasse le seuil.
- Pas de persistance — un restart de l'app remet les compteurs à zéro, ce qui
  est acceptable pour une protection brute-force (le redémarrage est déjà
  une mitigation naturelle).
"""
from __future__ import annotations

import threading as _threading
import time as _time
from collections import defaultdict, deque

# Config par défaut : 60 req/min = 1 req/s en moyenne.
# L'agent réel pull /pending toutes les 5s → 12 req/min → très loin du seuil.
DEFAULT_LIMIT = 60           # requêtes
DEFAULT_WINDOW_SECONDS = 60  # fenêtre glissante

_lock = _threading.Lock()
_tracker: dict[str, deque[float]] = defaultdict(deque)
# GC : on purge les entrées inactives depuis > 5× la fenêtre pour éviter
# de garder indéfiniment les deques vides après un agent déconnecté.
_last_gc_ts: float = 0.0
_GC_INTERVAL = 300.0         # 5 min
_INACTIVE_TTL = 300.0        # 5× la fenêtre 60s


def check(
    key: str,
    *,
    limit: int = DEFAULT_LIMIT,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> tuple[bool, int]:
    """Enregistre un hit et vérifie si la limite est dépassée.

    Returns:
        (allowed, retry_after_seconds) — si ``allowed=False``, le caller
        doit renvoyer 429 avec ``Retry-After: retry_after_seconds``.
        Si ``allowed=True``, ``retry_after_seconds=0``.
    """
    now = _time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        bucket = _tracker[key]
        # Purge des timestamps hors fenêtre
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        # Vérif seuil AVANT d'ajouter
        if len(bucket) >= limit:
            # Temps à attendre = fin de validité du plus ancien timestamp
            retry_after = max(1, int(window_seconds - (now - bucket[0])))
            return False, retry_after
        bucket.append(now)
        # GC opportuniste (évite une tâche de fond dédiée)
        global _last_gc_ts
        if now - _last_gc_ts > _GC_INTERVAL:
            _last_gc_ts = now
            _gc_inactive_keys(now)
    return True, 0


def _gc_inactive_keys(now: float) -> None:
    """Purge les deques vides depuis > _INACTIVE_TTL (lock déjà tenu)."""
    to_remove = [
        k for k, d in _tracker.items()
        if not d or (d and d[-1] < now - _INACTIVE_TTL)
    ]
    for k in to_remove:
        del _tracker[k]


def reset(key: str | None = None) -> None:
    """Reset du tracker (tests + admin)."""
    with _lock:
        if key is None:
            _tracker.clear()
        else:
            _tracker.pop(key, None)


def state_snapshot() -> dict[str, int]:
    """Retourne {key → nombre de hits dans la fenêtre courante} (diagnostics)."""
    with _lock:
        return {k: len(d) for k, d in _tracker.items() if d}
