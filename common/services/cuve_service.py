"""
common/services/cuve_service.py
===============================
Registre des cuves de production + tables de calibration volume ↔ hauteur.

Deux fichiers statiques sous ``data/`` :
  - ``cuves.csv``        : registre — quelle cuve a quelle capacité (L).
  - ``regles_cuves.csv`` : calibration — points (capacité, volume_L,
    hauteur_cm) servant à interpoler la hauteur de règle pour un volume.

Une cuve = un nom (choisi par l'opérateur dans l'app) + une capacité.
Plusieurs cuves peuvent partager la même capacité (donc la même table de
calibration) — ex. Cuve 1 et Cuve 2 font toutes deux 5200 L.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger("ferment.cuve_service")

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# Cache module-level : fichiers statiques, lus une seule fois.
_CUVES_CACHE: dict[str, Any] | None = None


def get_cuves() -> dict[str, Any]:
    """Renvoie le registre des cuves + les tables de calibration.

    Forme du retour::

        {
            "cuves": [{"nom": "Cuve 1", "capacite_l": 5200}, ...],
            "calibration": {
                "5200": [{"volume_l": 0.0, "hauteur_cm": 0.0}, ...],
                "7200": [...],
            },
        }

    Les points de calibration sont triés par volume croissant — l'app
    iOS interpole linéairement entre deux points.
    """
    global _CUVES_CACHE
    if _CUVES_CACHE is not None:
        return _CUVES_CACHE

    cuves: list[dict[str, Any]] = []
    cuves_path = _DATA_DIR / "cuves.csv"
    if cuves_path.exists():
        with cuves_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                nom = (row.get("cuve") or "").strip()
                cap_raw = (row.get("capacite_L") or "").strip()
                if not nom or not cap_raw:
                    continue
                try:
                    cuves.append({"nom": nom, "capacite_l": int(cap_raw)})
                except ValueError:
                    _log.warning("cuves.csv : capacité invalide %r", cap_raw)
    else:
        _log.warning("data/cuves.csv introuvable")

    calibration: dict[str, list[dict[str, float]]] = {}
    rules_path = _DATA_DIR / "regles_cuves.csv"
    if rules_path.exists():
        with rules_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    cap = str(int(float(row["cuve"])))
                    point = {
                        "volume_l": float(row["volume_L"]),
                        "hauteur_cm": float(row["hauteur_cm"]),
                    }
                except (KeyError, ValueError):
                    continue
                calibration.setdefault(cap, []).append(point)
    else:
        _log.warning("data/regles_cuves.csv introuvable")

    for points in calibration.values():
        points.sort(key=lambda p: p["volume_l"])

    _CUVES_CACHE = {"cuves": cuves, "calibration": calibration}
    return _CUVES_CACHE
