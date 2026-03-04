"""
common/easybeer/stocks.py
=========================
Stock-related endpoints: autonomie, MP lots, stock detail, carton weights.
"""
from __future__ import annotations

import datetime
import os
import time
from typing import Any

import requests

from ._client import (
    BASE,
    TIMEOUT,
    EasyBeerError,
    _auth,
    _check_response,
    _excel_payload,
    _indicator_payload,
    _log,
    retry_api,
)

# ─── Autonomie stocks ────────────────────────────────────────────────────────

@retry_api
def get_autonomie_stocks_excel(window_days: int) -> bytes:
    """POST /indicateur/autonomie-stocks/export/excel → Bytes du fichier Excel."""
    r = requests.post(
        f"{BASE}/indicateur/autonomie-stocks/export/excel",
        json=_excel_payload(window_days),
        auth=_auth(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.content


@retry_api
def get_autonomie_stocks(window_days: int) -> dict[str, Any]:
    """POST /indicateur/autonomie-stocks → JSON avec autonomie par produit fini."""
    r = requests.post(
        f"{BASE}/indicateur/autonomie-stocks",
        params={"forceRefresh": False},
        json=_indicator_payload(window_days),
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "autonomie-stocks")
    return r.json()


# ─── Lots matieres premieres ─────────────────────────────────────────────────

@retry_api
def get_mp_lots(id_matiere_premiere: int) -> list[dict[str, Any]]:
    """GET /stock/matieres-premieres/numero-lot/liste/{id} → Liste des lots."""
    ep = f"matieres-premieres/numero-lot/liste/{id_matiere_premiere}"
    r = requests.get(
        f"{BASE}/stock/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    data = r.json()
    return data if isinstance(data, list) else []


# ─── Detail stock produit ────────────────────────────────────────────────────

@retry_api
def get_stock_produit_detail(id_stock_produit: int) -> dict[str, Any]:
    """GET /stock/produit/edition/{id} → Detail complet d'un stock produit."""
    r = requests.get(
        f"{BASE}/stock/produit/edition/{id_stock_produit}",
        auth=_auth(),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ─── Poids cartons (avec cache fichier) ──────────────────────────────────────

_WEIGHTS_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "_carton_weights_cache.json",
)
_WEIGHTS_CACHE_TTL = 24 * 3600  # 24 heures


def _load_weights_cache() -> dict[tuple[int, str], float] | None:
    """Charge le cache fichier des poids cartons si encore valide."""
    import json
    try:
        with open(_WEIGHTS_CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
        ts = cache.get("ts", 0)
        if datetime.datetime.now(datetime.UTC).timestamp() - ts > _WEIGHTS_CACHE_TTL:
            return None
        weights: dict[tuple[int, str], float] = {}
        for entry in cache.get("data", []):
            weights[(entry["pid"], entry["fmt"])] = entry["w"]
        return weights
    except (OSError, ValueError, KeyError):
        _log.debug("Erreur chargement cache poids cartons", exc_info=True)
        return None


def _save_weights_cache(weights: dict[tuple[int, str], float]) -> None:
    """Sauvegarde le cache fichier des poids cartons (ecriture atomique via rename)."""
    import json
    import tempfile
    data = [{"pid": pid, "fmt": fmt, "w": w} for (pid, fmt), w in weights.items()]
    cache = {"ts": datetime.datetime.now(datetime.UTC).timestamp(), "data": data}
    try:
        cache_dir = os.path.dirname(_WEIGHTS_CACHE_PATH)
        os.makedirs(cache_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp_path, _WEIGHTS_CACHE_PATH)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except (OSError, ValueError):
        _log.warning("Impossible de sauvegarder le cache poids cartons", exc_info=True)


def fetch_carton_weights() -> dict[tuple[int, str], float]:
    """Recupere les poids cartons depuis EasyBeer (avec cache fichier 24h)."""
    cached = _load_weights_cache()
    if cached is not None:
        _log.debug("Cache poids cartons valide (%d entrees)", len(cached))
        return cached

    _log.info("Fetch poids cartons depuis EasyBeer (cache expire ou absent)")

    payload = {"idBrasserie": int(os.environ.get("EASYBEER_ID_BRASSERIE", "0"))}
    r = requests.post(
        f"{BASE}/stock/produits",
        json=payload,
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, "stock/produits")
    data = r.json()

    weights: dict[tuple[int, str], float] = {}
    for prod in data.get("consolidationsFilles", []):
        for conso in prod.get("consolidationsFilles", []):
            sid = conso.get("id")
            if not sid:
                continue

            produit = conso.get("produit") or {}
            id_produit = produit.get("idProduit")
            lot = conso.get("lot") or {}
            cont = conso.get("contenant") or {}
            contenance = float(cont.get("contenance", 0) or 0)
            lot_qty = int(lot.get("quantite", 0) or 0)
            if not (id_produit and contenance and lot_qty):
                continue

            fmt_str = f"{lot_qty}x{int(contenance * 100)}"

            try:
                detail = get_stock_produit_detail(sid)
                poids = float(detail.get("poidsUnitaire", 0) or 0)
                if poids > 0:
                    weights[(id_produit, fmt_str)] = poids
            except (EasyBeerError, requests.RequestException) as _e:
                _log.warning("Erreur fetch detail stock %s", sid, exc_info=True)

            time.sleep(0.3)

    _log.info("Fetch poids cartons termine : %d poids recuperes", len(weights))
    _save_weights_cache(weights)
    return weights
