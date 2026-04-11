"""
common/ramasse_grid.py
======================
Helpers purs pour la page ramasse : transformation des lignes métier vers le
format tableau Quasar, calculs palettes/poids, séparateurs par goût.

Ne dépend ni de NiceGUI ni de la base de données. Testable unitairement.
"""
from __future__ import annotations

import math
from typing import Any

from common.ramasse import PALETTE_EMPTY_WEIGHT


def safe_int(v: Any, default: int = 0) -> int:
    """Conversion robuste en int avec fallback (accepte str, float, None).

    Exemples ::

        safe_int("15.3") == 15
        safe_int(None) == 0
        safe_int("abc") == 0
        safe_int(None, default=99) == 99
    """
    try:
        return int(float(v)) if v is not None and v != "" else default
    except (TypeError, ValueError):
        return default


def compute_palettes_and_weight(
    cartons: int, poids_carton: float, palette_capacity: int
) -> tuple[int, int]:
    """Retourne (nb_palettes, poids_total_kg) pour un nombre de cartons donné.

    Formule :
        nb_palettes = ceil(cartons / palette_capacity) si cartons > 0 et capacity > 0
        poids_total = cartons × poids_carton + nb_palettes × PALETTE_EMPTY_WEIGHT

    Utilisée partout dans la page ramasse pour garantir la cohérence des calculs.
    """
    if palette_capacity > 0 and cartons > 0:
        nb_pal = math.ceil(cartons / palette_capacity)
    else:
        nb_pal = 0
    poids = int(round(cartons * poids_carton + nb_pal * PALETTE_EMPTY_WEIGHT, 0))
    return nb_pal, poids


def format_poids_display(poids_kg: int) -> str:
    """Formate le poids pour affichage : '1 250 kg' ou '—' si zéro."""
    if not poids_kg:
        return "—"
    return f"{poids_kg:,} kg".replace(",", " ")


def prepare_grid_rows(
    rows: list[dict[str, Any]], meta_by_label: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Transforme les lignes métier (build_ramasse_lines) en format tableau Quasar.

    Chaque grid_row contient :
    - les champs d'affichage (ref, produit, _gout, ddm, cartons, palettes, poids, poids_display)
    - les métadonnées métier (_poids_u, _pal_cap) nécessaires aux recalculs inline

    Le goût (``_gout``) est extrait du label produit : ``"Kéfir Original — 12x33cl"``
    → ``"Kéfir Original"``.
    """
    grid_rows: list[dict[str, Any]] = []
    for r in rows:
        label = r["Produit (goût + format)"]
        meta = meta_by_label.get(label, {})
        gout = label.split(" — ")[0].strip() if " — " in label else label
        ddm_val = r["DDM"]
        ddm_str = ddm_val.strftime("%d/%m/%Y") if hasattr(ddm_val, "strftime") else str(ddm_val)
        grid_rows.append({
            "ref": r["Référence"],
            "produit": label,
            "_gout": gout,
            "ddm": ddm_str,
            "cartons": None,
            "poids_u": float(meta.get("_poids_carton", 0)),
            "pal_cap": int(meta.get("_palette_capacity", 0)),
            "palettes": 0,
            "poids": 0,
            "poids_display": "—",
        })
    return grid_rows


def apply_saved_cartons(
    grid_rows: list[dict[str, Any]], saved_cartons: dict[str, int]
) -> None:
    """Restaure les valeurs de cartons précédemment saisies (par ref) dans grid_rows.

    Mute les lignes en place et recalcule palettes / poids / poids_display pour
    chaque ligne restaurée. Les lignes non présentes dans saved_cartons restent
    inchangées (cartons=None).
    """
    for row in grid_rows:
        ref = row["ref"]
        if ref not in saved_cartons:
            continue
        c = int(saved_cartons[ref])
        row["cartons"] = c
        pal, poids = compute_palettes_and_weight(
            c, float(row.get("poids_u") or 0), int(row.get("pal_cap") or 0)
        )
        row["palettes"] = pal
        row["poids"] = poids
        row["poids_display"] = format_poids_display(poids)


def insert_gout_separators(
    grid_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Insère des lignes "séparateur de goût" (``_sep=True``) avant chaque nouveau goût.

    Les grid_rows doivent être triés par ``_gout`` avant appel. Retourne une nouvelle
    liste incluant les séparateurs, sans muter l'entrée. Les séparateurs sont des
    dicts minimaux avec ``_sep=True`` pour distinguer en template Vue.
    """
    ordered: list[dict[str, Any]] = []
    current_gout = None
    for row in grid_rows:
        if row["_gout"] != current_gout:
            current_gout = row["_gout"]
            ordered.append({
                "_sep": True,
                "_gout": current_gout,
                "ref": f"_sep_{current_gout}",
                "produit": "", "ddm": "",
                "cartons": None, "palettes": 0,
                "poids": 0, "poids_display": "",
                "poids_u": 0, "pal_cap": 0,
            })
        ordered.append(row)
    return ordered
