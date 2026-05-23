"""
common/services/loading_eb_bind.py
==================================
⚠️  MODULE DÉPRÉCIÉ — NE PAS ACTIVER EN PROD  ⚠️

Premier essai (Sprint 2 quinque, 2026-05-23) de branchement finalize ramasse
→ event ``stock.sortie`` vers EB. **Conceptuellement faux** : ce code modélise
SOFRIPA comme un *client* EB destinataire d'un mouvement de sortie.

Modèle métier réel (clarifié 2026-05-23) :
- SOFRIPA est le **stock déporté de Ferment Station**, PAS un client.
- Easybeer ne gère pas de double entrepôt : le stock EB = le stock SOFRIPA.
- Une ramasse (transport Ferment → SOFRIPA) n'a PAS d'impact comptable EB.
- Le vrai mouvement comptable se fait au Conditionner (cf. Sprint 2 ter à venir,
  qui poussera un POST /brassin/mise-en-bouteille).

Le module est conservé pour :
1. Référence (mapping gtin → idProduit via la matrice code-barre, qui pourra
   être réutilisé pour le Conditionner)
2. Tests qui restent verts (n'importe pas le module dans le finalize_loading)

Mais **le feature flag ``EB_OUTBOX_BIND_LOADINGS`` ne doit JAMAIS être activé
en prod tel quel**. Il n'y a plus de caller actif depuis le commit qui a retiré
le hook dans common/services/loading_service.py.

Cf. docs/architecture-audit.md §3.2 — SOFRIPA = stock déporté.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

_log = logging.getLogger("ferment.loading_eb_bind")

if TYPE_CHECKING:
    from common.services.loading_service import PaletteInfo


# ─── Feature flag + config ────────────────────────────────────────────────


def is_eb_bind_enabled() -> bool:
    """True si le branchement EB ramasse est activé via env var.

    Distinct du flag fiches production (``EB_OUTBOX_BIND_PRODUCTION_SHEETS``)
    pour permettre d'activer les deux indépendamment lors du rollout.
    """
    return os.getenv("EB_OUTBOX_BIND_LOADINGS", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _get_warehouse_id() -> int | None:
    """Retourne l'idEntrepot Ferment depuis EB_DEFAULT_WAREHOUSE_ID, ou None."""
    raw = os.getenv("EB_DEFAULT_WAREHOUSE_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        _log.warning("EB_DEFAULT_WAREHOUSE_ID=%r non numérique — skip", raw)
        return None


def _get_sofripa_client_id() -> int | None:
    """Retourne l'idClient SOFRIPA depuis EB_SOFRIPA_CLIENT_ID, ou None."""
    raw = os.getenv("EB_SOFRIPA_CLIENT_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        _log.warning("EB_SOFRIPA_CLIENT_ID=%r non numérique — skip", raw)
        return None


# ─── Index gtin → idProduit ───────────────────────────────────────────────


def build_gtin_to_id_produit_index(raw_matrice: dict[str, Any]) -> dict[str, int]:
    """Construit l'index inverse {gtin_normalisé: idProduit} depuis la matrice EB.

    raw_matrice = sortie de ``common.easybeer.conditioning.get_code_barre_matrice()``.

    Le GTIN est normalisé (digits only) pour comparer avec ``gtin_palette`` qui
    arrive parfois préfixé d'un caractère AI GS1 ou avec des espaces.
    """
    index: dict[str, int] = {}
    for prod_entry in raw_matrice.get("produits", []) or []:
        for cb in prod_entry.get("codesBarres", []) or []:
            id_produit = (cb.get("modeleProduit") or {}).get("idProduit")
            code_raw = str(cb.get("code") or "")
            if not (id_produit and code_raw):
                continue
            digits = re.sub(r"\D+", "", code_raw)
            if not digits:
                continue
            # On indexe sur le GTIN complet ET les 6 derniers digits
            # (la "ref6" interne SOFRIPA). Permet de matcher dans les deux sens.
            index[digits] = int(id_produit)
            if len(digits) >= 6:
                index.setdefault(digits[-6:], int(id_produit))
    return index


def _normalize_gtin(value: str | None) -> str:
    """Retourne uniquement les digits d'un GTIN (suppression AI GS1, espaces)."""
    if not value:
        return ""
    return re.sub(r"\D+", "", value)


# ─── Builder du payload ──────────────────────────────────────────────────


def build_stock_sortie_payload(
    *,
    palettes: list[PaletteInfo],
    gtin_to_id_produit: dict[str, int],
    id_entrepot: int,
    id_client: int,
    date_ramasse: str,
    ramasse_numero: int | None,
    destinataire: str,
) -> tuple[dict[str, Any], list[str]]:
    """Construit un payload ModeleStockSortieForm pour une ramasse complète.

    Retourne ``(payload, warnings)`` où ``warnings`` liste les palettes
    qu'on a dû skipper (typiquement faute d'idProduit dans l'index).
    """
    elements: list[dict[str, Any]] = []
    warnings: list[str] = []

    for palette in palettes:
        gtin = _normalize_gtin(palette.gtin_palette) or _normalize_gtin(
            getattr(palette, "gtin_uvc", "")
        )
        if not gtin:
            warnings.append(f"SSCC {palette.sscc[-8:]}: pas de GTIN exploitable")
            continue

        id_produit = gtin_to_id_produit.get(gtin)
        if id_produit is None and len(gtin) >= 6:
            id_produit = gtin_to_id_produit.get(gtin[-6:])
        if id_produit is None:
            warnings.append(
                f"SSCC {palette.sscc[-8:]} (GTIN {gtin[-13:]}): idProduit introuvable",
            )
            continue

        element: dict[str, Any] = {
            "produit": {"idProduit": id_produit},
            "entrepot": {"idEntrepot": id_entrepot},
            "quantite": int(palette.case_count or 0),
        }
        if palette.lot:
            element["modeleNumerosLots"] = [{"numeroLot": palette.lot}]
        elements.append(element)

    libelle_parts = ["Ramasse"]
    if ramasse_numero is not None:
        libelle_parts.append(f"#{ramasse_numero}")
    libelle_parts.append(date_ramasse)
    if destinataire:
        libelle_parts.append(f"→ {destinataire}")

    payload: dict[str, Any] = {
        "dateFormulaire": _coerce_date_formulaire(date_ramasse),
        "libelle": " ".join(libelle_parts)[:200],
        "elements": elements,
        # Note : ``type`` (ModeleStockSortieType) est laissé à EB pour qu'il
        # applique le type par défaut. Si nécessaire, l'admin peut le forcer
        # via EB_DEFAULT_SORTIE_TYPE_ID (non implémenté ici).
    }
    type_id = os.getenv("EB_DEFAULT_SORTIE_TYPE_ID", "").strip()
    if type_id:
        try:
            payload["type"] = {"idStockSortieType": int(type_id)}
        except ValueError:
            _log.warning("EB_DEFAULT_SORTIE_TYPE_ID=%r non numérique — ignoré", type_id)

    # Lien client (SOFRIPA) — placé hors "elements" car ModeleStockSortieForm
    # n'a pas de champ client direct. Stocké dans le libelle pour traçabilité.
    # Note : si EB exige un client sur la sortie, on l'ajoutera dans une PR
    # suivante (peut nécessiter un payload différent ou un autre endpoint).
    _ = id_client  # consommé via libelle ; gardé en signature pour usage futur

    return payload, warnings


def _coerce_date_formulaire(date_ramasse: str) -> str:
    """Convertit YYYY-MM-DD en ISO datetime midi UTC.

    EB attend du datetime ; on prend midi par convention (date de ramasse,
    pas l'heure exacte qu'on n'a pas toujours).
    """
    if date_ramasse and re.match(r"^\d{4}-\d{2}-\d{2}$", date_ramasse):
        return f"{date_ramasse}T12:00:00"
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


# ─── Point d'entrée principal ─────────────────────────────────────────────


def enqueue_eb_events_from_loading(
    *,
    palettes: list[PaletteInfo],
    ramasse_id: str,
    ramasse_numero: int | None,
    date_ramasse: str,
    destinataire: str,
    tenant_id: str,
    user_email: str,
) -> dict[str, Any]:
    """Enqueue un event ``stock.sortie`` pour la ramasse finalisée.

    Best-effort : aucune exception ne remonte. Retourne un dict de résumé.
    Appelé depuis ``finalize_loading`` après la transition status='definitif'.
    """
    summary: dict[str, Any] = {
        "enabled": is_eb_bind_enabled(),
        "enqueued": [],
        "skipped": [],
        "errors": [],
        "warnings": [],
    }

    if not summary["enabled"]:
        summary["skipped_reason"] = "EB_OUTBOX_BIND_LOADINGS not enabled"
        _log.debug("EB bind loading disabled — skip ramasse %s", ramasse_id)
        return summary

    if not palettes:
        summary["skipped_reason"] = "no palettes loaded"
        return summary

    id_entrepot = _get_warehouse_id()
    id_client = _get_sofripa_client_id()
    if id_entrepot is None or id_client is None:
        summary["skipped_reason"] = (
            "EB_DEFAULT_WAREHOUSE_ID ou EB_SOFRIPA_CLIENT_ID non configuré"
        )
        _log.warning(
            "EB bind loading: ramasse %s skipped (entrepot=%s, client=%s)",
            ramasse_id, id_entrepot, id_client,
        )
        return summary

    # Charger l'index GTIN → idProduit depuis le cache code-barre matrice
    try:
        from common.easybeer.conditioning import get_code_barre_matrice
        matrice = get_code_barre_matrice()
        gtin_index = build_gtin_to_id_produit_index(matrice)
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"matrice: {type(exc).__name__}: {exc}")
        _log.exception("EB bind loading: ramasse %s — failed to load matrice", ramasse_id)
        return summary

    if not gtin_index:
        summary["skipped_reason"] = "code-barre matrice empty"
        return summary

    # Construire le payload
    try:
        payload, warnings = build_stock_sortie_payload(
            palettes=palettes,
            gtin_to_id_produit=gtin_index,
            id_entrepot=id_entrepot,
            id_client=id_client,
            date_ramasse=date_ramasse,
            ramasse_numero=ramasse_numero,
            destinataire=destinataire,
        )
        summary["warnings"].extend(warnings)
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"build_payload: {type(exc).__name__}: {exc}")
        _log.exception("EB bind loading: ramasse %s — build_payload failed", ramasse_id)
        return summary

    if not payload.get("elements"):
        summary["skipped_reason"] = "no element exploitable (tous gtin non mappés)"
        return summary

    # Enqueue via outbox
    try:
        from common.easybeer.queued import enqueue_stock_sortie
        eid = enqueue_stock_sortie(
            tenant_id=tenant_id,
            payload=payload,
            user_email=user_email,
        )
        if eid is not None:
            summary["enqueued"].append({
                "event_type": "stock.sortie",
                "id": eid,
                "elements_count": len(payload["elements"]),
            })
            _log.info(
                "EB bind: ramasse %s → enqueue stock.sortie (outbox id=%s, %d elements)",
                ramasse_id, eid, len(payload["elements"]),
            )
        else:
            summary["errors"].append("enqueue_stock_sortie returned None")
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"enqueue: {type(exc).__name__}: {exc}")
        _log.exception("EB bind: ramasse %s — enqueue failed", ramasse_id)

    return summary
