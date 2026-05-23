"""
common/services/production_sheet_eb_bind.py
===========================================
Branchement du finalize fiche production vers Easybeer via l'outbox.

Quand l'opérateur finalise une fiche production sur iOS, ce module enqueue
automatiquement les événements EB correspondants — au lieu d'attendre que
le responsable d'atelier les saisisse manuellement dans EB.

Pattern :
- Feature flag ``EB_OUTBOX_BIND_PRODUCTION_SHEETS`` (env var) — OFF par
  défaut pour permettre un déploiement progressif
- Le push échoue toujours en silence (best-effort) — la finalize locale
  ne doit jamais échouer à cause d'un problème de mapping/outbox
- Le worker outbox s'occupe du retry exponentiel + dead-letter

Branchements actifs :
- ``brassin.mesure`` (Sprint 2 bis) : dernière mesure de la section
  ``fermentation`` poussée vers ``POST /brassin/mesure/enregistrer``
  (avec incident si nonConformite)
- ``brassin.mise-en-bouteille`` (Sprint 2 ter) : items de
  ``conditionnement_reel`` poussés vers ``POST /brassin/mise-en-bouteille``
  (Conditionner — crée le stock produit côté EB)

Branchements à venir :
- ``brassin.terminer`` : nécessite charger le brassin EB complet d'abord
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

_log = logging.getLogger("ferment.production_sheet_eb_bind")

if TYPE_CHECKING:
    from common.services.production_sheet_service import ProductionSheetDetail


# ─── Feature flag ─────────────────────────────────────────────────────────


def is_eb_bind_enabled() -> bool:
    """True si le branchement EB est activé via env var.

    Désactivé par défaut pour permettre un rollout progressif :
    - On déploie le code (flag OFF) → aucun impact runtime
    - On active le flag en prod (env var) → les nouvelles finalize poussent vers EB
    - Si problème : on désactive le flag, on garde le contrôle
    """
    return os.getenv("EB_OUTBOX_BIND_PRODUCTION_SHEETS", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ─── Builders de payload ──────────────────────────────────────────────────


def build_mesure_payload(
    sheet: ProductionSheetDetail,
    *,
    user_email: str,
) -> dict[str, Any] | None:
    """Construit le payload ModeleBrassinMesure depuis la dernière mesure de fermentation.

    Retourne None si la fiche n'a pas de brassin_id ou pas de mesure exploitable.

    Stratégie : on prend la dernière mesure (la plus récente) de la section
    ``fermentation``. Si la fiche a une note d'incident, on la pousse via le
    champ ``nonConformite`` de la mesure (= incident côté EB).
    """
    if not sheet.brassin_id:
        return None

    try:
        brassin_id = int(sheet.brassin_id)
    except (ValueError, TypeError):
        _log.warning(
            "Sheet %s : brassin_id=%r non numérique, skip mesure EB",
            sheet.id, sheet.brassin_id,
        )
        return None

    data = sheet.data or {}
    fermentation = data.get("fermentation") or {}
    mesures = fermentation.get("mesures") or []

    if not mesures:
        return None

    last = mesures[-1]
    if not isinstance(last, dict):
        return None

    # Mesure brute, en gardant les champs présents uniquement
    payload: dict[str, Any] = {
        "idBrassin": brassin_id,
        "etape": "fermentation",
        "auteur": user_email or last.get("matricule") or "",
        "dateFormulaire": _coerce_date_formulaire(last),
    }

    # Mesures numériques optionnelles
    if (brix := _safe_float(last.get("brix"))) is not None:
        payload["densite"] = brix
    if (ph := _safe_float(last.get("ph"))) is not None:
        payload["ph"] = ph
    if (temp := _safe_float(last.get("temperature"))) is not None:
        payload["temperature"] = temp

    # Commentaire métier (goût + observation)
    commentaire_parts = []
    if g := (last.get("gout") or "").strip():
        commentaire_parts.append(f"Goût : {g}")
    if o := (last.get("observation") or "").strip():
        commentaire_parts.append(f"Observation : {o}")
    if commentaire_parts:
        payload["commentaire"] = " — ".join(commentaire_parts)

    # Incident : si la fiche a une note dans la section incidents,
    # on la pousse dans nonConformite (= incident côté EB)
    incidents = data.get("incidents") or {}
    incident_notes = (incidents.get("notes") or "").strip()
    statut_ferm = (fermentation.get("statut") or "").strip().lower()
    if incident_notes or statut_ferm == "non conforme":
        nc_text = incident_notes or "Non conforme (sans détail)"
        payload["nonConformite"] = nc_text[:500]  # safety cap

    return payload


def build_mise_en_bouteille_payload(
    sheet: ProductionSheetDetail,
    *,
    tenant_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Construit le payload ModeleStockProduit depuis la section ``conditionnement_reel``.

    Retourne ``(payload, warnings)``. ``payload`` peut être None si la fiche
    n'a pas de brassin_id ou si aucun item n'est résolvable côté EB.
    ``warnings`` liste les items qui ont été skipped (mapping EB manquant).

    Mapping appliqué pour chaque item :
    - ``(lot, marque, fmt)`` → ``(idProduit, idContenant, contenance_l, pcb)``
      via ``eb_product_mapping.resolve_lot_marque_fmt``
    - ``quantite_bouteilles`` = ``cartons × pcb``
    - 1 entrée ``ModeleStockProduitBouteille`` par item

    NOTE V1 : on N'inclut PAS ``idStockBouteille`` (le stock de contenant
    vide consommé) parce qu'on n'a pas cette info côté iOS. EB devra soit
    auto-résoudre, soit refuser le payload (auquel cas on verra l'erreur
    dans le dashboard ``/admin/eb-outbox``). Itérer si besoin.
    """
    warnings: list[str] = []

    if not sheet.brassin_id:
        return None, ["no brassin_id (manual sheet)"]
    try:
        brassin_id = int(sheet.brassin_id)
    except (ValueError, TypeError):
        return None, [f"brassin_id={sheet.brassin_id!r} not numeric"]

    data = sheet.data or {}
    cond_reel = data.get("conditionnement_reel") or {}
    items = cond_reel.get("items") or []
    if not items:
        return None, ["no conditionnement_reel.items"]

    lot = sheet.lot or ""
    if not lot:
        return None, ["sheet.lot is empty (Conditionner requires numeroLot)"]

    # Charger la matrice EB une seule fois pour tous les items
    from common.services.eb_product_mapping import (
        load_gtin_index_from_eb,
        resolve_lot_marque_fmt,
    )
    gtin_index = load_gtin_index_from_eb()
    if not gtin_index:
        return None, ["code-barre matrice unavailable"]

    bouteille_entries: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        marque = (item.get("marque") or "").strip()
        fmt = (item.get("fmt") or "").strip()
        cartons = item.get("cartons")
        if not (marque and fmt and cartons):
            warnings.append(f"item incomplete (marque={marque!r}, fmt={fmt!r}, cartons={cartons!r})")
            continue
        try:
            cartons_int = int(cartons)
        except (ValueError, TypeError):
            warnings.append(f"cartons not numeric for ({marque}, {fmt})")
            continue
        if cartons_int <= 0:
            continue

        resolution = resolve_lot_marque_fmt(
            tenant_id=tenant_id,
            lot=lot,
            marque=marque,
            fmt=fmt,
            gtin_index=gtin_index,
        )
        if resolution is None:
            warnings.append(
                f"unresolved (lot={lot}, marque={marque}, fmt={fmt}): "
                "no etiquette_palette_history match or gtin not in EB matrice",
            )
            continue
        if resolution.pcb <= 0:
            warnings.append(f"pcb=0 for ({marque}, {fmt}) — skip")
            continue

        quantite_bouteilles = cartons_int * resolution.pcb
        entry: dict[str, Any] = {
            "modeleProduit": {"idProduit": resolution.id_produit},
            "quantiteMiseEnBouteille": quantite_bouteilles,
        }
        if resolution.contenance_l is not None:
            entry["contenance"] = resolution.contenance_l
        bouteille_entries.append(entry)

    if not bouteille_entries:
        return None, warnings + ["no item resolvable to EB references"]

    # Date de mise en bouteille = finalized_at si dispo, sinon now
    from datetime import UTC, datetime
    if sheet.finalized_at:
        date_mise = sheet.finalized_at.strftime("%Y-%m-%dT%H:%M:%S")
    else:
        date_mise = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")

    payload: dict[str, Any] = {
        "dateMiseEnBouteille": date_mise,
        "numeroLot": lot,
        "modeleBrassin": {"id": brassin_id},
        "modelesStockProduitBouteille": bouteille_entries,
    }
    if sheet.ddm:
        payload["dateLimiteUtilisationOptimale"] = sheet.ddm.strftime(
            "%Y-%m-%dT00:00:00",
        )
    return payload, warnings


# ─── Point d'entrée principal ─────────────────────────────────────────────


def enqueue_eb_events_from_sheet(
    sheet: ProductionSheetDetail,
    *,
    tenant_id: str,
    user_email: str,
) -> dict[str, Any]:
    """Enqueue les events EB correspondant à une fiche production finalisée.

    Best-effort : aucune exception ne remonte. Retourne un dict de résumé
    pour log/observabilité :
        {"skipped_reason": "...", "enqueued": ["brassin.mesure"], ...}

    Appelé depuis ``finalize_sheet`` après la finalize locale réussie.
    """
    summary: dict[str, Any] = {
        "enabled": is_eb_bind_enabled(),
        "enqueued": [],
        "skipped": [],
        "errors": [],
    }

    if not summary["enabled"]:
        summary["skipped_reason"] = "EB_OUTBOX_BIND_PRODUCTION_SHEETS not enabled"
        _log.debug("EB bind disabled — skip finalize push for sheet %s", sheet.id)
        return summary

    if not sheet.brassin_id:
        summary["skipped_reason"] = "no brassin_id (manual sheet)"
        _log.info(
            "EB bind: sheet %s has no brassin_id (manual sheet) — nothing to push",
            sheet.id,
        )
        return summary

    # ─── 1. Mesure (fermentation) ─────────────────────────────────────
    try:
        from common.easybeer.queued import enqueue_brassin_mesure

        mesure_payload = build_mesure_payload(sheet, user_email=user_email)
        if mesure_payload is None:
            summary["skipped"].append("brassin.mesure (no exploitable measurement)")
        else:
            eid = enqueue_brassin_mesure(
                tenant_id=tenant_id,
                payload=mesure_payload,
                user_email=user_email,
            )
            if eid is not None:
                summary["enqueued"].append({"event_type": "brassin.mesure", "id": eid})
                _log.info(
                    "EB bind: sheet %s → enqueue brassin.mesure (outbox id=%s)",
                    sheet.id, eid,
                )
            else:
                summary["errors"].append("enqueue_brassin_mesure returned None")
    except Exception as exc:  # noqa: BLE001 — best-effort
        summary["errors"].append(f"brassin.mesure: {type(exc).__name__}: {exc}")
        _log.exception("EB bind: failed to enqueue mesure for sheet %s", sheet.id)

    # ─── 2. Mise en bouteille (Conditionner) ──────────────────────────
    try:
        from common.easybeer.queued import enqueue_brassin_mise_en_bouteille

        mise_payload, mise_warnings = build_mise_en_bouteille_payload(
            sheet, tenant_id=tenant_id,
        )
        if mise_warnings:
            summary.setdefault("warnings", []).extend(
                f"brassin.mise-en-bouteille: {w}" for w in mise_warnings
            )
        if mise_payload is None:
            summary["skipped"].append(
                "brassin.mise-en-bouteille (no resolvable item)",
            )
        else:
            eid = enqueue_brassin_mise_en_bouteille(
                tenant_id=tenant_id,
                payload=mise_payload,
                user_email=user_email,
            )
            if eid is not None:
                summary["enqueued"].append(
                    {
                        "event_type": "brassin.mise-en-bouteille",
                        "id": eid,
                        "items_count": len(mise_payload["modelesStockProduitBouteille"]),
                    },
                )
                _log.info(
                    "EB bind: sheet %s → enqueue brassin.mise-en-bouteille "
                    "(outbox id=%s, %d items)",
                    sheet.id, eid, len(mise_payload["modelesStockProduitBouteille"]),
                )
            else:
                summary["errors"].append(
                    "enqueue_brassin_mise_en_bouteille returned None",
                )
    except Exception as exc:  # noqa: BLE001 — best-effort
        summary["errors"].append(
            f"brassin.mise-en-bouteille: {type(exc).__name__}: {exc}",
        )
        _log.exception(
            "EB bind: failed to enqueue mise-en-bouteille for sheet %s",
            sheet.id,
        )

    # ─── 3. Terminer (TODO PR suivante) ───────────────────────────────
    summary["skipped"].append("brassin.terminer (TODO: load full brassin first)")

    return summary


# ─── Helpers ──────────────────────────────────────────────────────────────


def _safe_float(value: Any) -> float | None:
    """Convertit en float, retourne None si impossible ou vide."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_date_formulaire(measure: dict[str, Any]) -> str:
    """Retourne une string YYYY-MM-DDTHH:MM:00 si possible, sinon now()."""
    from datetime import UTC, datetime

    date_str = (measure.get("date") or "").strip()
    heure_str = (measure.get("heure") or "00:00").strip()

    if date_str:
        # On accepte YYYY-MM-DD ou ISO complet, on prend juste la date
        try:
            d = date_str.split("T")[0]
            h = heure_str if ":" in heure_str else f"{heure_str}:00"
            return f"{d}T{h}:00"
        except Exception:  # noqa: BLE001
            pass
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
