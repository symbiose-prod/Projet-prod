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
- ``brassin.terminer`` (Sprint 2 quater) : termine + archive le brassin
  côté EB. **Activé uniquement si la fiche a le flag explicite**
  ``data.brassin_termine == True`` (à ajouter côté iOS dans une PR
  ultérieure). Le payload reste léger (overrides) — le worker charge
  le ModeleBrassin complet à l'appel (lazy load, retry-safe).
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
    """Construit le payload **léger** pour l'event outbox ``brassin.mise-en-bouteille``.

    Le worker ``mise_en_bouteille_brassin`` (cf. ``common.easybeer.production_writes``)
    se charge ensuite, au moment du push à EB, de :

    1. ``get_brassin_detail(idBrassin)`` → brassin complet
    2. Résoudre ``idStockBouteille`` pour chaque item (via
       ``common.services.bottle_stock_resolver.resolve_bottle_stock``)
    3. ``POST /brassin/deduction-stocks-conditionnement`` → EB calcule
       ``modelesStocksMiseEnBouteille`` (capsules, étiquettes, cartons à débiter)
    4. ``POST /brassin/mise-en-bouteille`` avec le payload complet

    Cette séparation builder/worker garde le payload outbox léger (~500 octets
    au lieu de 50+ KB), évite la stale data, et survit aux retries.

    Args:
        sheet: la fiche finalisée (status=completed, data.conditionnement_reel
            non vide).
        tenant_id: scope multi-tenant. Pas utilisé directement ici mais
            propagé dans le payload outbox pour le worker (qui s'en sert
            pour les lookups de la table eb_stock_product_templates).

    Returns:
        Tuple ``(payload, warnings)`` :
        - ``payload`` : ``dict`` à enqueue dans outbox, ou ``None`` si
          conditions de déclenchement non remplies.
        - ``warnings`` : liste de messages pour observabilité / dead-letter
          report (items skippés, configuration manquante, etc.).

    Conditions de skip (retourne None) :
    - ``sheet.brassin_id`` absent ou non-numérique
    - ``sheet.data.conditionnement_reel.items`` vide
    - ``sheet.lot`` vide (EB exige ``numeroLot``)

    Source de vérité du format : ``docs/easybeer-write-payloads/mise-en-bouteille.request.json``.
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
    items_raw = cond_reel.get("items") or []
    if not items_raw:
        return None, ["no conditionnement_reel.items"]

    lot = sheet.lot or ""
    if not lot:
        return None, ["sheet.lot is empty (mise-en-bouteille requires numeroLot)"]

    # On normalise les items : (fmt, marque, cartons). Le worker s'occupera
    # de la résolution idStockBouteille via la table eb_stock_product_templates.
    normalized_items: list[dict[str, Any]] = []
    for item in items_raw:
        if not isinstance(item, dict):
            continue
        marque = (item.get("marque") or "").strip()
        fmt = (item.get("fmt") or "").strip()
        cartons = item.get("cartons")
        if not (marque and fmt and cartons):
            warnings.append(
                f"item incomplete (marque={marque!r}, fmt={fmt!r}, cartons={cartons!r})",
            )
            continue
        try:
            cartons_int = int(cartons)
        except (ValueError, TypeError):
            warnings.append(f"cartons not numeric for ({marque}, {fmt})")
            continue
        if cartons_int <= 0:
            continue
        normalized_items.append({
            "marque": marque,
            "fmt": fmt,
            "cartons": cartons_int,
        })

    if not normalized_items:
        return None, warnings + ["no valid item in conditionnement_reel"]

    # Date ISO format EB UI (ex "2026-05-24T06:54:41.063Z")
    from datetime import UTC, datetime
    dt = sheet.finalized_at or datetime.now(UTC)
    date_mise_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # DDM : préférer la valeur figée de la fiche, sinon laisser le worker la
    # dériver depuis brassin.produit.durabiliteMinimale au moment du push.
    payload: dict[str, Any] = {
        "idBrassin": brassin_id,
        "tenantId": tenant_id,  # nécessaire au worker pour le lookup DB templates
        "numeroLot": lot,
        "dateMiseEnBouteille": date_mise_iso,
        "items": normalized_items,
    }
    if sheet.ddm:
        # Format ISO "YYYY-MM-DDTHH:MM:SS.000Z" (cohérent avec EB UI)
        payload["dateLimiteUtilisationOptimale"] = sheet.ddm.strftime(
            "%Y-%m-%dT00:00:00.000Z",
        )
    return payload, warnings


def build_terminer_payload(
    sheet: ProductionSheetDetail,
    *,
    tenant_id: str = "",
    user_email: str = "",
) -> dict[str, Any] | None:
    """Construit un payload riche (overrides) pour ``POST /brassin/terminer``.

    **Conditions de déclenchement** :
    - sheet.brassin_id numérique
    - ``sheet.data.brassin_termine`` est explicitement à ``True``
      (sinon retourne None pour skipper)

    Le worker outbox chargera le ModeleBrassin EB complet au moment du push
    (lazy load via ``terminer_brassin``), puis appliquera ces overrides.

    Champs poussés (cf. payload de référence EB UI dans
    ``docs/easybeer-write-payloads/terminer.request.json``) :
    - ``idBrassin`` : identifiant brassin (top-level, conforme EB ;
      attention, EB n'utilise PAS ``id`` mais ``idBrassin``)
    - ``archive`` : ``True`` si ``data.archiver`` est à True
    - ``dateFinFormulaire`` : ISO date de fin (format EB UI)
    - ``volumeFinal`` : Σ (cartons × pcb × contenance) depuis conditionnement_reel
    - ``densiteInitiale`` : première mesure de fermentation
    - ``densiteFinale`` : dernière mesure de fermentation
    - ``ph`` : dernière mesure
    - ``commentaire`` : récap HTML riche (mesures, incidents, conditionnement,
      **liste des SSCC** pour traçabilité GS1, remarques)
    """
    if not sheet.brassin_id:
        return None
    try:
        brassin_id = int(sheet.brassin_id)
    except (ValueError, TypeError):
        return None

    data = sheet.data or {}
    if not data.get("brassin_termine"):
        # Pas de flag explicite → on ne touche pas au brassin EB
        return None

    # Date ISO (format EB UI : "2026-05-21T22:00:00.000Z")
    from datetime import UTC, datetime
    dt = sheet.finalized_at or datetime.now(UTC)
    date_fin_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    overrides: dict[str, Any] = {
        # EB UI envoie idBrassin au top-level (PAS id). Notre worker en lazy
        # mode utilisera cette valeur pour fetch + merge le brassin complet.
        "idBrassin": brassin_id,
        "dateFinFormulaire": date_fin_iso,
        "archive": bool(data.get("archiver", False)),
    }

    # ─── Mesures finales depuis fermentation (dernière mesure) ─────────
    fermentation = data.get("fermentation") or {}
    mesures = fermentation.get("mesures") or []
    if mesures:
        first = mesures[0] if isinstance(mesures[0], dict) else {}
        last = mesures[-1] if isinstance(mesures[-1], dict) else {}

        # Densités : première = initiale, dernière = finale
        if (di := _safe_float(first.get("brix"))) is not None:
            overrides["densiteInitiale"] = di
        if (df := _safe_float(last.get("brix"))) is not None:
            overrides["densiteFinale"] = df
        if (ph := _safe_float(last.get("ph"))) is not None:
            overrides["ph"] = ph
        if (temp := _safe_float(last.get("temperature"))) is not None:
            overrides["temperature"] = temp

    # ─── Volume final depuis conditionnement_reel ─────────────────────
    volume_final = _compute_volume_final(data)
    if volume_final is not None:
        overrides["volumeFinal"] = volume_final

    # ─── Commentaire HTML riche (avec SSCC pour traçabilité GS1) ──────
    html_commentaire = _build_commentaire_html(
        sheet,
        data=data,
        user_email=user_email,
        tenant_id=tenant_id,
    )
    if html_commentaire:
        overrides["commentaire"] = html_commentaire

    return overrides


def _fetch_sscc_for_lot(tenant_id: str, lot: str) -> list[dict[str, Any]]:
    """Charge la liste des SSCC actifs pour ce lot.

    Joint ``sscc_log`` (source de vérité GS1, avec voided_at) avec
    ``etiquette_palette_history`` (metadata marque/fmt/designation).

    Retourne une liste de dicts avec : sscc, gtin_palette, lot, ddm,
    case_count, generated_at, marque, fmt, designation, gout.

    Best-effort : retourne [] si DB indisponible ou tenant/lot vide.
    """
    if not (tenant_id and lot):
        return []
    try:
        from db.conn import run_sql
        rows = run_sql(
            """
            SELECT
              sl.sscc,
              sl.gtin_palette,
              sl.lot,
              sl.ddm,
              sl.case_count,
              sl.generated_at,
              eph.marque,
              eph.fmt,
              eph.designation,
              eph.gout
            FROM sscc_log sl
            LEFT JOIN etiquette_palette_history eph
              ON eph.sscc = sl.sscc
             AND eph.tenant_id = sl.tenant_id
            WHERE sl.tenant_id = :tid
              AND sl.lot = :lot
              AND sl.voided_at IS NULL
            ORDER BY sl.generated_at ASC
            """,
            {"tid": tenant_id, "lot": lot},
        ) or []
        return rows
    except Exception:
        _log.exception("EB bind: failed to fetch SSCC for lot %s", lot)
        return []


def _compute_volume_final(data: dict[str, Any]) -> float | None:
    """Calcule le volume final conditionné en L depuis ``conditionnement_reel.items``.

    Formule : Σ (cartons × pcb × contenance_l) pour chaque format.
    Retourne None si pas calculable (pas d'items ou pas d'info contenance).

    Note : contenance dérivée de ``fmt`` (ex : "12x33" → 0.33 L).
    """
    cond_reel = data.get("conditionnement_reel") or {}
    items = cond_reel.get("items") or []
    if not items:
        return None

    total_l = 0.0
    found_any = False
    for item in items:
        if not isinstance(item, dict):
            continue
        cartons = _safe_float(item.get("cartons"))
        if cartons is None or cartons <= 0:
            continue
        fmt = (item.get("fmt") or "").lower().replace(" ", "")
        # fmt format "Nxcc" ex: "12x33" → 12 bouteilles de 33cl
        if "x" not in fmt:
            continue
        try:
            pcb_str, vol_cl_str = fmt.split("x", 1)
            pcb = int(pcb_str)
            vol_cl = int(vol_cl_str)
            if pcb <= 0 or vol_cl <= 0:
                continue
            contenance_l = vol_cl / 100.0
            total_l += cartons * pcb * contenance_l
            found_any = True
        except (ValueError, TypeError):
            continue

    return round(total_l, 2) if found_any else None


def _build_commentaire_html(
    sheet: ProductionSheetDetail,
    *,
    data: dict[str, Any],
    user_email: str,
    tenant_id: str = "",
) -> str:
    """Construit un commentaire HTML riche pour l'archivage du brassin.

    Sections :
    1. Méta (auteur, date, lot)
    2. Mesures de fermentation (liste chronologique)
    3. Incidents éventuels
    4. Conditionnement réel (récap)
    5. **Palettes / SSCC** (traçabilité GS1) — depuis sscc_log + etiquette_palette_history
    6. Remarques libres
    """
    from html import escape

    parts: list[str] = []

    # 1. Méta
    parts.append("<h3>Récapitulatif fiche production</h3>")
    meta_lines = [f"<b>Lot</b> : {escape(sheet.lot or '—')}"]
    if user_email:
        meta_lines.append(f"<b>Auteur</b> : {escape(user_email)}")
    if sheet.finalized_at:
        meta_lines.append(
            f"<b>Finalisé le</b> : {sheet.finalized_at.strftime('%d/%m/%Y %H:%M')}",
        )
    parts.append("<p>" + " — ".join(meta_lines) + "</p>")

    # 2. Mesures fermentation
    fermentation = data.get("fermentation") or {}
    mesures = fermentation.get("mesures") or []
    if mesures:
        parts.append("<h4>Mesures de fermentation</h4>")
        parts.append("<ul>")
        for m in mesures:
            if not isinstance(m, dict):
                continue
            date_str = (m.get("date") or "") + " " + (m.get("heure") or "")
            measures_parts = []
            if (brix := m.get("brix")):
                measures_parts.append(f"Densité {escape(str(brix))}")
            if (ph := m.get("ph")):
                measures_parts.append(f"pH {escape(str(ph))}")
            if (temp := m.get("temperature")):
                measures_parts.append(f"T° {escape(str(temp))}°C")
            obs = (m.get("observation") or "").strip()
            gout = (m.get("gout") or "").strip()
            note_extra = []
            if gout:
                note_extra.append(f"Goût : {escape(gout)}")
            if obs:
                note_extra.append(f"Obs : {escape(obs)}")
            line = (
                f"<li><i>{escape(date_str.strip())}</i> — "
                f"{' / '.join(measures_parts) if measures_parts else '—'}"
            )
            if note_extra:
                line += f" — {' ; '.join(note_extra)}"
            line += "</li>"
            parts.append(line)
        parts.append("</ul>")
        # Statut fermentation
        statut = (fermentation.get("statut") or "").strip()
        if statut:
            parts.append(f"<p><b>Statut fermentation</b> : {escape(statut)}</p>")

    # 3. Incidents
    incidents = data.get("incidents") or {}
    notes = (incidents.get("notes") or "").strip()
    photos = incidents.get("photos") or []
    if notes or photos:
        parts.append("<h4>Incidents</h4>")
        if notes:
            parts.append(f"<p>{escape(notes)}</p>")
        if photos:
            parts.append(
                f"<p><i>{len(photos)} photo(s) attachée(s) à la fiche locale</i></p>",
            )

    # 4. Conditionnement réel
    cond_reel = data.get("conditionnement_reel") or {}
    items = cond_reel.get("items") or []
    if items:
        parts.append("<h4>Conditionnement réel</h4>")
        parts.append("<ul>")
        for item in items:
            if not isinstance(item, dict):
                continue
            marque = item.get("marque") or "?"
            fmt = item.get("fmt") or "?"
            cartons = item.get("cartons") or 0
            designation = (item.get("designation") or "").strip()
            line = f"<li><b>{escape(str(marque))} {escape(str(fmt))}</b> — {escape(str(cartons))} cartons"
            if designation:
                line += f" ({escape(designation)})"
            line += "</li>"
            parts.append(line)
        parts.append("</ul>")

    # 5. Palettes / SSCC (traçabilité GS1)
    if tenant_id and sheet.lot:
        sscc_rows = _fetch_sscc_for_lot(tenant_id, sheet.lot)
        if sscc_rows:
            parts.append(f"<h4>Palettes / SSCC ({len(sscc_rows)} palettes)</h4>")
            parts.append("<ul>")
            for row in sscc_rows:
                sscc = str(row.get("sscc") or "")
                marque = str(row.get("marque") or "")
                fmt = str(row.get("fmt") or "")
                designation = str(row.get("designation") or "")
                gout = str(row.get("gout") or "")
                case_count = row.get("case_count")
                generated_at = row.get("generated_at")

                line_parts = [f"<code>{escape(sscc)}</code>"]
                if marque or fmt:
                    line_parts.append(
                        f"<b>{escape(marque)} {escape(fmt)}</b>".strip()
                    )
                if designation or gout:
                    label = " ".join(s for s in (designation, gout) if s).strip()
                    line_parts.append(escape(label))
                if case_count:
                    line_parts.append(f"{escape(str(case_count))} cartons")
                if generated_at:
                    try:
                        date_str = generated_at.strftime("%d/%m/%Y %H:%M")
                        line_parts.append(f"<i>{escape(date_str)}</i>")
                    except (AttributeError, ValueError):
                        pass
                parts.append("<li>" + " — ".join(line_parts) + "</li>")
            parts.append("</ul>")

    # 6. Remarques libres
    remarques = (data.get("remarques") or "").strip()
    if remarques:
        parts.append("<h4>Remarques</h4>")
        parts.append(f"<p>{escape(remarques)}</p>")

    html = "".join(parts)
    # Safety cap (EB n'a pas de limite documentée mais on évite les payloads géants)
    if len(html) > 10_000:
        html = html[:10_000] + "<p><i>(commentaire tronqué)</i></p>"
    return html


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

    # ─── 3. Terminer (Sprint 2 quater) ────────────────────────────────
    try:
        from common.easybeer.queued import enqueue_brassin_terminer

        terminer_payload = build_terminer_payload(
            sheet, tenant_id=tenant_id, user_email=user_email,
        )
        if terminer_payload is None:
            summary["skipped"].append(
                "brassin.terminer (no data.brassin_termine flag — skip)",
            )
        else:
            eid = enqueue_brassin_terminer(
                tenant_id=tenant_id,
                payload=terminer_payload,
                user_email=user_email,
            )
            if eid is not None:
                summary["enqueued"].append({
                    "event_type": "brassin.terminer",
                    "id": eid,
                    "archive": terminer_payload.get("archive", False),
                })
                _log.info(
                    "EB bind: sheet %s → enqueue brassin.terminer "
                    "(outbox id=%s, archive=%s)",
                    sheet.id, eid, terminer_payload.get("archive"),
                )
            else:
                summary["errors"].append("enqueue_brassin_terminer returned None")
    except Exception as exc:  # noqa: BLE001 — best-effort
        summary["errors"].append(f"brassin.terminer: {type(exc).__name__}: {exc}")
        _log.exception(
            "EB bind: failed to enqueue terminer for sheet %s", sheet.id,
        )

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
