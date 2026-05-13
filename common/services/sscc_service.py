"""
common/services/sscc_service.py
================================
Génération de SSCC (Serial Shipping Container Code) — identifiant unique
de palette logistique selon la norme GS1.

Structure du SSCC (18 chiffres strict, norme GS1) :

    [E][P P P P P P P P P][S S S S S S S][C]
     1            9               7        1
     ext   préfixe entreprise   séquentiel   clé

  - E : chiffre d'extension (1 chiffre, libre).
        ⚠ CONVENTION INTERNE FERMENT STATION (pas une règle GS1) :
          - 1 = carton (unité logistique secondaire) — futur
          - 3 = palette (unité logistique principale)  — implémenté ici
        Le chiffre d'extension distingue visuellement le type d'unité
        sans avoir à interpréter la structure du code.
  - P : préfixe entreprise GS1 (9 chiffres — Ferment Station = 377001442)
  - S : compteur séquentiel persistant (7 chiffres = 10M valeurs, atomic
        via PostgreSQL SEQUENCE, NO CYCLE — jamais réutilisé)
  - C : clé de contrôle modulo 10 GS1 (positions alternées ×3 et ×1
        en partant de la droite)

⚠ Note : la spec initiale donnée par l'utilisateur indiquait "séquentiel
8 chiffres", mais ça donnerait 1+9+8+1 = 19, ce qui n'est PAS un SSCC
valide (la norme impose strictement 18). On utilise 7 chiffres pour le
séquentiel, ce qui couvre 10M palettes (suffisant à perpétuité).

Encodage GS1-128 : ``(00)<18-digit-SSCC>``

Référence : GS1 General Specifications, Section 3 — Application
Identifiers, AI 00 (SSCC).

Ce module est sans NiceGUI : utilisable depuis CLI / cron / tests.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass

from db.conn import run_sql

_log = logging.getLogger("ferment.services.sscc")


# ─── Constantes métier ──────────────────────────────────────────────────────

# Convention interne Ferment Station (PAS une règle GS1 officielle) :
# le chiffre d'extension permet de distinguer visuellement le type d'unité
# logistique sans avoir à interpréter la structure du code.
SSCC_EXTENSION_PALETTE = "3"      # palette filmée (unité logistique principale)
SSCC_EXTENSION_CARTON = "1"       # carton individuel (futur — pas encore implémenté)

# Préfixe entreprise GS1 attribué à Ferment Station / Symbiose Kéfir.
# Inscrit auprès de GS1 France — ne JAMAIS le changer sans coordination.
SSCC_COMPANY_PREFIX = "377001442"  # 9 chiffres

# Alias rétro-compat (anciennes versions du code/tests) — pointe sur PALETTE
# par défaut car c'est le seul cas d'usage actuel.
SSCC_EXTENSION_DIGIT = SSCC_EXTENSION_PALETTE

_SSCC_SERIAL_LEN = 7              # 7 chiffres → ext + prefix + serial = 17 → +1 check = 18


# ─── Modèles typés ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SsccGenResult:
    """Résultat d'une génération SSCC : code + payload GS1-128 prêt à imprimer."""
    sscc: str                # 18 digits
    pretty: str              # ex: "3377 0014 4200 0000 05" (groupé pour œil)
    gs1_data: str            # ex: "(00)337700144200000005" (entrée treepoem)
    hri: str                 # ex: "(00) 3 37700 14420 00000 05" (lisible humain)


@dataclass(frozen=True)
class SsccLogEntry:
    """Une ligne du journal SSCC pour affichage / export audit."""
    id: int
    sscc: str
    user_email: str
    gtin_palette: str
    lot: str
    ddm: _dt.date | None
    case_count: int
    generated_at: _dt.datetime
    voided_at: _dt.datetime | None = None
    voided_reason: str = ""
    voided_by: str = ""
    # Lien vers la ramasse si la palette a été chargée (via palette_loadings)
    ramasse_id: str = ""
    ramasse_date: _dt.date | None = None
    ramasse_destinataire: str = ""
    loaded_at: _dt.datetime | None = None


# ─── Algorithme clé de contrôle GS1 (pure) ──────────────────────────────────

def gs1_check_digit(digits: str) -> int:
    """Calcule la clé de contrôle modulo 10 GS1 pour une chaîne de digits.

    Algorithme standard GS1 (s'applique à GTIN-8/12/13/14, SSCC, GLN, etc.) :
      1. En partant du chiffre le plus à droite, alterner les poids 3 puis 1.
      2. Sommer tous les produits.
      3. Clé = (10 - (somme mod 10)) mod 10.

    Args:
        digits: chaîne de digits ASCII sans la clé finale.

    Raises:
        ValueError: si ``digits`` contient des non-digits ou est vide.
    """
    if not digits or not digits.isdigit():
        raise ValueError(f"gs1_check_digit : chaîne de digits attendue, reçu {digits!r}")
    total = 0
    for i, c in enumerate(reversed(digits)):
        weight = 3 if i % 2 == 0 else 1
        total += int(c) * weight
    return (10 - (total % 10)) % 10


def format_sscc_pretty(sscc18: str) -> str:
    """Formate un SSCC 18 digits en groupes pour la lecture humaine.

    Ex: "337700144200000005" → "3377 0014 4200 0000 05"
    """
    s = re.sub(r"\D+", "", sscc18 or "")
    if len(s) != 18:
        return s
    return f"{s[0:4]} {s[4:8]} {s[8:12]} {s[12:16]} {s[16:18]}"


def _build_sscc_from_serial(serial: int, extension: str = SSCC_EXTENSION_PALETTE) -> str:
    """Construit un SSCC 18 digits complet à partir d'un numéro séquentiel.

    Pure function : ne touche pas à la DB. Exposée pour les tests.

    Args:
        serial: numéro séquentiel (≥ 0, ≤ 9 999 999).
        extension: chiffre d'extension (1 char digit). Default = palette.
            Pour les cartons (futur), passer ``SSCC_EXTENSION_CARTON``.
    """
    if not (0 <= serial < 10**_SSCC_SERIAL_LEN):
        raise ValueError(
            f"SSCC serial hors bornes [0, {10**_SSCC_SERIAL_LEN - 1}] : {serial}",
        )
    if len(extension) != 1 or not extension.isdigit():
        raise ValueError(f"extension doit être 1 chiffre, reçu {extension!r}")
    serial_str = str(serial).zfill(_SSCC_SERIAL_LEN)
    body17 = f"{extension}{SSCC_COMPANY_PREFIX}{serial_str}"
    if len(body17) != 17:
        raise RuntimeError(
            f"SSCC body invalide ({len(body17)} digits, attendu 17) — "
            "vérifier extension + SSCC_COMPANY_PREFIX + _SSCC_SERIAL_LEN",
        )
    return f"{body17}{gs1_check_digit(body17)}"


# ─── Persistance : compteur + audit ─────────────────────────────────────────

def _next_sscc_serial() -> int:
    """Atomic : récupère le prochain numéro séquentiel via la sequence DB.

    PostgreSQL garantit que `nextval()` est atomic même sous concurrence.
    En cas de rollback de la transaction appelante, le numéro est PERDU
    (jamais réutilisé) — c'est exactement ce qu'on veut pour un SSCC.
    """
    rows = run_sql("SELECT nextval('sscc_serial_seq') AS n")
    return int(rows[0]["n"])


def generate_sscc(
    tenant_id: str,
    *,
    user_email: str = "",
    gtin_palette: str = "",
    lot: str = "",
    ddm: _dt.date | None = None,
    case_count: int = 0,
) -> SsccGenResult:
    """Génère un nouveau SSCC unique et l'enregistre dans le log audit.

    Le numéro séquentiel est tiré atomiquement de ``sscc_serial_seq``,
    garantissant qu'aucun SSCC n'est jamais réutilisé même en cas de
    concurrence ou de crash serveur. L'INSERT dans sscc_log est best-effort :
    en cas d'échec on log mais on retourne quand même le SSCC (déjà
    attribué par la séquence, ne peut pas être annulé).

    Args:
        tenant_id: UUID du tenant (pour audit).
        user_email: email de l'opérateur (audit).
        gtin_palette: GTIN-14 du carton contenu (audit).
        lot: numéro de lot (audit).
        ddm: date de durabilité minimale (audit).
        case_count: nombre de cartons sur la palette (audit).
    """
    serial = _next_sscc_serial()
    sscc = _build_sscc_from_serial(serial)

    try:
        run_sql(
            """INSERT INTO sscc_log
               (sscc, tenant_id, user_email, gtin_palette, lot, ddm, case_count)
               VALUES (:sscc, :t, :u, :g, :l, :d, :c)""",
            {
                "sscc": sscc,
                "t": tenant_id,
                "u": user_email or "",
                "g": gtin_palette or "",
                "l": lot or "",
                "d": ddm,
                "c": int(case_count or 0),
            },
        )
    except Exception:
        _log.exception("Échec INSERT sscc_log — SSCC attribué mais non audité")

    return SsccGenResult(
        sscc=sscc,
        pretty=format_sscc_pretty(sscc),
        gs1_data=f"(00){sscc}",
        hri=f"(00) {sscc[0]} {sscc[1:6]} {sscc[6:11]} {sscc[11:16]} {sscc[16:18]}",
    )


def list_sscc_log(
    tenant_id: str,
    *,
    date_from: _dt.date | None = None,
    date_to: _dt.date | None = None,
    lot_filter: str = "",
    limit: int = 500,
) -> list[SsccLogEntry]:
    """Liste les SSCC générés selon les filtres demandés.

    Args:
        tenant_id: scope tenant
        date_from: inclus (>=)
        date_to: inclus (<= fin de journée)
        lot_filter: ILIKE %motif% sur la colonne lot
        limit: hard cap pour éviter de tout charger en mémoire

    Returns:
        Liste triée par date desc (le plus récent en haut).
    """
    where = ["sl.tenant_id = :t"]
    params: dict = {"t": tenant_id, "lim": int(limit)}
    if date_from:
        where.append("sl.generated_at >= :df")
        params["df"] = date_from
    if date_to:
        where.append("sl.generated_at < (:dt::date + INTERVAL '1 day')")
        params["dt"] = date_to
    if lot_filter:
        where.append("sl.lot ILIKE :lot")
        params["lot"] = f"%{lot_filter.strip()}%"
    # JOIN palette_loadings + ramasse_history pour récupérer le lien
    # SSCC → ramasse (None si pas encore chargé).
    sql = f"""
        SELECT sl.id, sl.sscc, sl.user_email, sl.gtin_palette, sl.lot, sl.ddm,
               sl.case_count, sl.generated_at,
               sl.voided_at, sl.voided_reason, sl.voided_by,
               pl.ramasse_id AS pl_ramasse_id,
               pl.scanned_at AS pl_loaded_at,
               rh.date_ramasse AS rh_date,
               rh.destinataire AS rh_destinataire
        FROM sscc_log sl
        LEFT JOIN palette_loadings pl
               ON pl.sscc = sl.sscc AND pl.tenant_id = sl.tenant_id
        LEFT JOIN ramasse_history rh
               ON rh.id = pl.ramasse_id
        WHERE {" AND ".join(where)}
        ORDER BY sl.generated_at DESC
        LIMIT :lim
    """
    try:
        rows = run_sql(sql, params) or []
    except Exception:
        _log.exception("Échec list_sscc_log")
        return []
    out: list[SsccLogEntry] = []
    for r in rows:
        try:
            ddm = r.get("ddm")
            rh_date = r.get("rh_date")
            out.append(SsccLogEntry(
                id=int(r["id"]),
                sscc=str(r["sscc"] or ""),
                user_email=str(r.get("user_email") or ""),
                gtin_palette=str(r.get("gtin_palette") or ""),
                lot=str(r.get("lot") or ""),
                ddm=ddm if isinstance(ddm, _dt.date) or ddm is None
                    else _dt.date.fromisoformat(str(ddm)[:10]),
                case_count=int(r.get("case_count") or 0),
                generated_at=r["generated_at"],
                voided_at=r.get("voided_at"),
                voided_reason=str(r.get("voided_reason") or ""),
                voided_by=str(r.get("voided_by") or ""),
                ramasse_id=str(r.get("pl_ramasse_id") or ""),
                ramasse_date=rh_date if isinstance(rh_date, _dt.date) or rh_date is None
                    else _dt.date.fromisoformat(str(rh_date)[:10]),
                ramasse_destinataire=str(r.get("rh_destinataire") or ""),
                loaded_at=r.get("pl_loaded_at"),
            ))
        except (KeyError, TypeError, ValueError):
            _log.warning("Ligne sscc_log invalide ignorée : %r", r, exc_info=True)
    return out


def void_sscc(
    tenant_id: str, sscc: str, *, reason: str, user_email: str = "",
) -> bool:
    """Marque un SSCC comme annulé (palette fantôme — étiquette pas
    imprimée, doublon, etc.).

    Le séquentiel reste consommé pour rester conforme GS1 (jamais de
    réutilisation < 1 an). L'enregistrement est gardé pour audit ; il
    sera juste filtré des lookups normaux (scan chargement, panier,
    palettes non chargées récentes).

    Args:
        tenant_id: scope
        sscc: 18 digits du SSCC à annuler
        reason: raison saisie par l'opérateur (obligatoire, ≤ 500 chars)
        user_email: qui annule (audit)

    Returns:
        True si annulation effective, False si SSCC introuvable ou
        déjà annulé.
    """
    s = re.sub(r"\D+", "", sscc or "")
    if len(s) != 18:
        return False
    reason_clean = (reason or "").strip()[:500]
    if not reason_clean:
        reason_clean = "Sans raison précisée"
    try:
        rows = run_sql(
            """UPDATE sscc_log SET
                  voided_at = now(),
                  voided_reason = :r,
                  voided_by = :u
               WHERE sscc = :s AND tenant_id = :t
                 AND voided_at IS NULL
               RETURNING id""",
            {"s": s, "t": tenant_id, "r": reason_clean, "u": user_email or ""},
        )
        if rows:
            _log.warning(
                "SSCC voided : sscc=%s tenant=%s by=%s reason=%s",
                s, tenant_id, user_email or "?", reason_clean,
            )
            return True
        return False
    except Exception:
        _log.exception("Échec void_sscc sscc=%s", s)
        return False


def restore_sscc(tenant_id: str, sscc: str) -> bool:
    """Annule une annulation — pour les cas où on s'est trompé.

    Réservé aux admins (UI à connecter plus tard si besoin).
    """
    s = re.sub(r"\D+", "", sscc or "")
    if len(s) != 18:
        return False
    try:
        rows = run_sql(
            """UPDATE sscc_log SET
                  voided_at = NULL, voided_reason = NULL, voided_by = NULL
               WHERE sscc = :s AND tenant_id = :t
                 AND voided_at IS NOT NULL
               RETURNING id""",
            {"s": s, "t": tenant_id},
        )
        return bool(rows)
    except Exception:
        _log.exception("Échec restore_sscc sscc=%s", s)
        return False


def get_sscc_stats(tenant_id: str) -> dict:
    """Compteurs rapides pour le dashboard : aujourd'hui / ce mois / total."""
    try:
        rows = run_sql(
            """SELECT
                  COUNT(*) FILTER (WHERE generated_at >= CURRENT_DATE) AS today,
                  COUNT(*) FILTER (WHERE generated_at >= date_trunc('month', CURRENT_DATE)) AS this_month,
                  COUNT(*) AS total
               FROM sscc_log
               WHERE tenant_id = :t""",
            {"t": tenant_id},
        )
    except Exception:
        _log.exception("Échec get_sscc_stats")
        return {"today": 0, "this_month": 0, "total": 0}
    r = (rows or [{}])[0]
    return {
        "today": int(r.get("today") or 0),
        "this_month": int(r.get("this_month") or 0),
        "total": int(r.get("total") or 0),
    }


def reconstruct_sscc_payload(sscc18: str) -> SsccGenResult:
    """Reconstruit un SsccGenResult depuis un SSCC déjà existant (réimpression).

    N'incrémente PAS la séquence, ne log RIEN. Utilisé quand on réimprime
    une étiquette dont le SSCC est déjà connu (stocké dans
    etiquette_palette_history).
    """
    s = re.sub(r"\D+", "", sscc18 or "")
    if len(s) != 18 or not s.isdigit():
        raise ValueError(f"SSCC invalide : attendu 18 digits, reçu {sscc18!r}")
    return SsccGenResult(
        sscc=s,
        pretty=format_sscc_pretty(s),
        gs1_data=f"(00){s}",
        hri=f"(00) {s[0]} {s[1:6]} {s[6:11]} {s[11:16]} {s[16:18]}",
    )
