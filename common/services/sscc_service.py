"""
common/services/sscc_service.py
================================
Génération de SSCC (Serial Shipping Container Code) — identifiant unique
de palette logistique selon la norme GS1.

Structure du SSCC (18 chiffres strict, norme GS1) :

    [E][P P P P P P P P P][S S S S S S S][C]
     1            9               7        1
     ext   préfixe entreprise   séquentiel   clé

  - E : chiffre d'extension (1 chiffre, libre — ici 3)
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

SSCC_EXTENSION_DIGIT = "3"        # 1 chiffre, libre
SSCC_COMPANY_PREFIX = "377001442"  # 9 chiffres, attribué par GS1 France
_SSCC_SERIAL_LEN = 7              # 7 chiffres → ext + prefix + serial = 17 → +1 check = 18


# ─── Modèles typés ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SsccGenResult:
    """Résultat d'une génération SSCC : code + payload GS1-128 prêt à imprimer."""
    sscc: str                # 18 digits
    pretty: str              # ex: "3377 0014 4200 0000 05" (groupé pour œil)
    gs1_data: str            # ex: "(00)337700144200000005" (entrée treepoem)
    hri: str                 # ex: "(00) 3 37700 14420 00000 05" (lisible humain)


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


def _build_sscc_from_serial(serial: int) -> str:
    """Construit un SSCC 18 digits complet à partir d'un numéro séquentiel.

    Pure function : ne touche pas à la DB. Exposée pour les tests.
    """
    if not (0 <= serial < 10**_SSCC_SERIAL_LEN):
        raise ValueError(
            f"SSCC serial hors bornes [0, {10**_SSCC_SERIAL_LEN - 1}] : {serial}",
        )
    serial_str = str(serial).zfill(_SSCC_SERIAL_LEN)
    body17 = f"{SSCC_EXTENSION_DIGIT}{SSCC_COMPANY_PREFIX}{serial_str}"
    if len(body17) != 17:
        raise RuntimeError(
            f"SSCC body invalide ({len(body17)} digits, attendu 17) — "
            "vérifier SSCC_EXTENSION_DIGIT + SSCC_COMPANY_PREFIX + _SSCC_SERIAL_LEN",
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
