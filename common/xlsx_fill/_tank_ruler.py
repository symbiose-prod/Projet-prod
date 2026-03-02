"""
common/xlsx_fill/_tank_ruler.py
===============================
Tank ruler interpolation from CSV data.
"""
from __future__ import annotations

from ._helpers import _project_root

# Cache module-level pour le CSV regles_cuves (fichier statique, lu une seule fois)
_RULER_CACHE: dict[int, tuple[list[float], list[float]]] | None = None


def _load_ruler_table() -> dict[int, tuple[list[float], list[float]]]:
    """Charge et indexe le CSV regles_cuves par capacite de cuve."""
    global _RULER_CACHE
    if _RULER_CACHE is not None:
        return _RULER_CACHE

    csv_path = _project_root() / "data" / "regles_cuves.csv"
    if not csv_path.exists():
        _RULER_CACHE = {}
        return _RULER_CACHE

    import pandas as _pd_ruler
    df = _pd_ruler.read_csv(csv_path)
    cache: dict[int, tuple[list[float], list[float]]] = {}
    for cap, grp in df.groupby("cuve"):
        grp_sorted = grp.sort_values("volume_L")
        cache[int(cap)] = (
            grp_sorted["volume_L"].tolist(),
            grp_sorted["hauteur_cm"].tolist(),
        )
    _RULER_CACHE = cache
    return _RULER_CACHE


def interpolate_ruler_height(volume_L: float, tank_capacity: int) -> float:
    """
    Interpole la hauteur de regle (cm) pour un volume donne dans une cuve.
    Utilise la table data/regles_cuves.csv (cachee en memoire apres 1er appel).
    """
    table = _load_ruler_table()
    entry = table.get(tank_capacity)
    if not entry:
        return 0.0

    volumes, heights = entry

    if volume_L <= volumes[0]:
        return float(heights[0])
    if volume_L >= volumes[-1]:
        return float(heights[-1])

    for i in range(len(volumes) - 1):
        if volumes[i] <= volume_L <= volumes[i + 1]:
            dv = volumes[i + 1] - volumes[i]
            if dv == 0:
                return float(heights[i])
            t = (volume_L - volumes[i]) / dv
            return round(heights[i] + t * (heights[i + 1] - heights[i]), 1)

    return float(heights[-1])
