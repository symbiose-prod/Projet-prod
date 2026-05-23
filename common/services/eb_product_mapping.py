"""
common/services/eb_product_mapping.py
=====================================
Mapping centralisé entre les données locales (fiches production, étiquettes
palette) et les références Easybeer (idProduit, idContenant).

Utilisé par les modules de bind EB (production_sheet_eb_bind, ...) pour
construire les payloads vers EB.

Stratégie :
1. La matrice code-barre EB (cachée localement via eb_sync_loop) contient
   pour chaque produit la liste des codes-barres avec leur idContenant et
   idProduit. On indexe par gtin pour lookup rapide.
2. `etiquette_palette_history` (table locale) lie ``(lot, marque, fmt)``
   → ``gtin_uvc`` + ``pcb``. C'est notre passerelle entre la sémantique
   métier locale (marque, fmt) et la sémantique EB (gtin → idProduit).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from db.conn import run_sql

_log = logging.getLogger("ferment.eb_product_mapping")


# ─── Dataclasses ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GtinIndexEntry:
    """Une entrée de la matrice EB indexée par gtin."""
    id_produit: int
    id_contenant: int | None
    contenance_l: float | None
    lot_libelle: str | None


@dataclass(frozen=True)
class LotMarqueFmtResolution:
    """Résolution d'un item (marque, fmt) sur un lot donné vers EB."""
    id_produit: int
    id_contenant: int | None
    contenance_l: float | None
    pcb: int
    gtin_uvc: str


# ─── Normalisation ────────────────────────────────────────────────────────


def normalize_gtin(value: str | None) -> str:
    """Retourne uniquement les digits d'un GTIN."""
    if not value:
        return ""
    return re.sub(r"\D+", "", value)


# ─── Index matrice EB ─────────────────────────────────────────────────────


def build_gtin_index(raw_matrice: dict[str, Any]) -> dict[str, GtinIndexEntry]:
    """Construit l'index ``{gtin_normalisé: GtinIndexEntry}`` depuis la matrice EB.

    raw_matrice = sortie de ``common.easybeer.conditioning.get_code_barre_matrice()``.

    Indexe à la fois le GTIN complet ET ses 6 derniers digits (ref6 SOFRIPA),
    pour matcher quel que soit le format reçu.
    """
    index: dict[str, GtinIndexEntry] = {}
    for prod_entry in raw_matrice.get("produits", []) or []:
        for cb in prod_entry.get("codesBarres", []) or []:
            id_produit = (cb.get("modeleProduit") or {}).get("idProduit")
            code_raw = str(cb.get("code") or "")
            if not (id_produit and code_raw):
                continue
            digits = normalize_gtin(code_raw)
            if not digits:
                continue

            mod_cont = cb.get("modeleContenant") or {}
            id_contenant = mod_cont.get("idContenant")
            contenance = mod_cont.get("contenance")
            mod_lot = cb.get("modeleLot") or {}
            lot_libelle = (mod_lot.get("libelle") or "").strip() or None

            entry = GtinIndexEntry(
                id_produit=int(id_produit),
                id_contenant=int(id_contenant) if id_contenant else None,
                contenance_l=float(contenance) if contenance else None,
                lot_libelle=lot_libelle,
            )
            # Index full gtin ET ref6
            index[digits] = entry
            if len(digits) >= 6:
                index.setdefault(digits[-6:], entry)
    return index


def lookup_gtin(gtin_index: dict[str, GtinIndexEntry], gtin: str) -> GtinIndexEntry | None:
    """Cherche un gtin dans l'index (essaie full puis ref6)."""
    digits = normalize_gtin(gtin)
    if not digits:
        return None
    if entry := gtin_index.get(digits):
        return entry
    if len(digits) >= 6:
        return gtin_index.get(digits[-6:])
    return None


# ─── Résolution (lot, marque, fmt) → EB ──────────────────────────────────


def _query_etiquette_history(
    tenant_id: str,
    *,
    lot: str,
    marque: str,
    fmt: str,
) -> dict[str, Any] | None:
    """Cherche la dernière étiquette palette générée pour ce (lot, marque, fmt).

    On part du principe que l'équipe a généré au moins une étiquette palette
    pour le lot/marque/fmt en cours de production. Si plusieurs étiquettes
    existent (palettes successives du même format), on prend la plus récente.

    Retourne le row dict ou None si rien trouvé.
    """
    rows = run_sql(
        """
        SELECT ean, gtin_uvc, lot, fmt, marque, designation, pcb
        FROM etiquette_palette_history
        WHERE tenant_id = :tid
          AND lot   = :lot
          AND marque = :marque
          AND fmt   = :fmt
        ORDER BY generated_at DESC
        LIMIT 1
        """,
        {"tid": tenant_id, "lot": lot, "marque": marque, "fmt": fmt},
    ) or []
    return rows[0] if rows else None


def resolve_lot_marque_fmt(
    *,
    tenant_id: str,
    lot: str,
    marque: str,
    fmt: str,
    gtin_index: dict[str, GtinIndexEntry],
) -> LotMarqueFmtResolution | None:
    """Résout un item (lot, marque, fmt) vers les références EB.

    Stratégie :
    1. Cherche dans ``etiquette_palette_history`` une étiquette générée pour
       ce (lot, marque, fmt) — récupère ``gtin_uvc`` et ``pcb``.
    2. Lookup ``gtin_uvc`` dans la matrice EB → ``(idProduit, idContenant, contenance)``.

    Retourne ``None`` si l'étiquette n'existe pas ou si le gtin n'est pas
    dans la matrice. L'appelant log un warning et skip ce item.
    """
    if not (lot and marque and fmt):
        return None

    eph = _query_etiquette_history(tenant_id, lot=lot, marque=marque, fmt=fmt)
    if eph is None:
        _log.debug(
            "EB mapping: no etiquette_palette_history for (lot=%s, marque=%s, fmt=%s)",
            lot, marque, fmt,
        )
        return None

    gtin_uvc = str(eph.get("gtin_uvc") or "")
    if not gtin_uvc:
        # Fallback sur le ean (GTIN colis carton) si pas de gtin_uvc — moins
        # précis car le carton ≠ la bouteille côté EB, mais mieux que rien.
        gtin_uvc = str(eph.get("ean") or "")
    if not gtin_uvc:
        return None

    entry = lookup_gtin(gtin_index, gtin_uvc)
    if entry is None:
        _log.debug(
            "EB mapping: gtin %s introuvable dans la matrice EB pour (lot=%s, marque=%s, fmt=%s)",
            gtin_uvc, lot, marque, fmt,
        )
        return None

    return LotMarqueFmtResolution(
        id_produit=entry.id_produit,
        id_contenant=entry.id_contenant,
        contenance_l=entry.contenance_l,
        pcb=int(eph.get("pcb") or 0),
        gtin_uvc=gtin_uvc,
    )


# ─── Chargement de la matrice (helper) ────────────────────────────────────


def load_gtin_index_from_eb() -> dict[str, GtinIndexEntry]:
    """Charge l'index gtin depuis la matrice code-barre EB (cache local TTL 24h).

    Renvoie un dict vide si la matrice est indisponible.
    """
    try:
        from common.easybeer.conditioning import get_code_barre_matrice
        matrice = get_code_barre_matrice()
        return build_gtin_index(matrice)
    except Exception:
        _log.exception("EB mapping: failed to load code-barre matrice")
        return {}
