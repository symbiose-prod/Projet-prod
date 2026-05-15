"""
common/services/scan_decoder.py
================================
Décodeur générique d'un scan code-barres.

Sert principalement à la page de test ``/test-douchette`` : permet à
l'opérateur de scanner n'importe quel code-barres (SSCC palette, EAN
carton, GS1-128 complet, QR code URL, etc.) et voir immédiatement
comment le système l'a interprété.

Côté production, les pages métier (``/chargement-camion``,
``/etiquettes-palette``) ont leur propre parseur dédié à leur cas
d'usage (SSCC ou EAN). Ce module est un Swiss-army knife pour le test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from common.services.etiquette_palette_service import (
    parse_gs1_digits,
    parse_gs1_string,
)


@dataclass(frozen=True)
class DecodedScan:
    """Résultat structuré d'un scan."""
    raw: str                              # chaîne brute reçue
    normalized: str                       # après nettoyage (FNC1 visible, etc.)
    type: str                             # 'sscc' | 'ean13' | 'gtin14' |
                                          # 'gs1_128_hri' | 'gs1_128_raw' |
                                          # 'url' | 'text' | 'empty'
    ais: dict[str, str] = field(default_factory=dict)  # AIs parsés si GS1-128
    sscc: str = ""                        # SSCC 18 digits si trouvé
    summary: str = ""                     # phrase humaine pour l'UI


# Caractère FNC1 ASCII GS — séparateur des AIs à longueur variable dans
# le GS1-128 brut. Beaucoup de douchettes l'émettent en clair, certains
# navigateurs le rendent invisible. On le remplace par '|' pour la
# visibilité dans la chaîne ``normalized``.
_FNC1 = "\x1d"
_FNC1_VISIBLE = "|"


def _normalize(raw: str) -> str:
    """Nettoyage léger : remplace FNC1 par un séparateur visible."""
    return (raw or "").replace(_FNC1, _FNC1_VISIBLE)


def decode_scan(raw: str) -> DecodedScan:
    """Analyse une chaîne reçue d'une douchette et retourne sa nature.

    Détection par cascade (le plus spécifique en premier) :

    1. Vide → ``empty``.
    2. URL (http/https) → ``url``.
    3. Avec parenthèses ``(AI)data`` → ``gs1_128_hri`` (format
       human-readable, comme imprimé sous le code-barres).
    4. Que des digits :
       a. 18 digits → ``sscc`` (SSCC palette nu).
       b. 13 digits → ``ean13`` (carton supermarché).
       c. 14 digits → ``gtin14`` (carton logistique).
       d. Si commence par un AI connu (00, 01, 02) → ``gs1_128_raw``
          (digits collés, FNC1 invisible) — parsé avec ``parse_gs1_digits``.
       e. Sinon → ``text`` (chaîne numérique non reconnue).
    5. Contient FNC1 brut → ``gs1_128_raw``.
    6. Sinon → ``text``.

    Pour les types GS1-128, ``ais`` contient les AIs trouvés et
    ``sscc`` est rempli si l'AI 00 est présent (utile : l'UI sait
    immédiatement « c'est un scan palette »).
    """
    if not raw:
        return DecodedScan(raw="", normalized="", type="empty", summary="Scan vide")

    normalized = _normalize(raw)
    stripped = raw.strip()

    # 1. URL ?
    if stripped.lower().startswith(("http://", "https://")):
        return DecodedScan(
            raw=raw, normalized=normalized, type="url",
            summary=f"URL : {stripped}",
        )

    # 2. GS1-128 HRI (parenthèses) ?
    if "(" in stripped and ")" in stripped:
        ais = parse_gs1_string(stripped)
        if ais:
            sscc = ais.get("00", "")
            summary = _build_gs1_summary(ais)
            return DecodedScan(
                raw=raw, normalized=normalized, type="gs1_128_hri",
                ais=ais, sscc=sscc, summary=summary,
            )

    # 3. FNC1 brut présent ?
    if _FNC1 in raw:
        # Essayer de parser. parse_gs1_digits ignore les non-digits donc
        # consomme le FNC1 silencieusement — convient ici.
        ais = parse_gs1_digits(raw)
        if ais:
            sscc = ais.get("00", "")
            return DecodedScan(
                raw=raw, normalized=normalized, type="gs1_128_raw",
                ais=ais, sscc=sscc, summary=_build_gs1_summary(ais),
            )
        return DecodedScan(
            raw=raw, normalized=normalized, type="text",
            summary=f"Chaîne avec FNC1 non parsable ({len(raw)} chars)",
        )

    # 4. Pure digits ?
    if stripped.isdigit():
        n = len(stripped)
        # SSCC 18 digits
        if n == 18:
            return DecodedScan(
                raw=raw, normalized=normalized, type="sscc",
                sscc=stripped,
                summary=f"SSCC palette : {_fmt_sscc(stripped)}",
            )
        # EAN-13 (carton supermarché classique)
        if n == 13:
            return DecodedScan(
                raw=raw, normalized=normalized, type="ean13",
                summary=f"EAN-13 : {stripped}",
            )
        # GTIN-14 (caisse logistique)
        if n == 14:
            return DecodedScan(
                raw=raw, normalized=normalized, type="gtin14",
                summary=f"GTIN-14 : {stripped}",
            )
        # Long digits commençant par un AI connu → GS1-128 raw
        if n > 16 and stripped[:2] in ("00", "01", "02"):
            ais = parse_gs1_digits(stripped)
            if ais:
                return DecodedScan(
                    raw=raw, normalized=normalized, type="gs1_128_raw",
                    ais=ais, sscc=ais.get("00", ""),
                    summary=_build_gs1_summary(ais),
                )
        # Digits non reconnus
        return DecodedScan(
            raw=raw, normalized=normalized, type="text",
            summary=f"{n} chiffres (format non reconnu)",
        )

    # 5. Texte libre
    return DecodedScan(
        raw=raw, normalized=normalized, type="text",
        summary=f"Texte : {stripped[:60]}{'…' if len(stripped) > 60 else ''}",
    )


# ─── Helpers d'affichage ────────────────────────────────────────────────────

def _fmt_sscc(sscc18: str) -> str:
    """Formate ``337700144200000005`` → ``3377 0014 4200 0000 05``."""
    s = re.sub(r"\D+", "", sscc18 or "")
    if len(s) != 18:
        return s
    return f"{s[0:4]} {s[4:8]} {s[8:12]} {s[12:16]} {s[16:18]}"


# Libellés humains pour les AIs courants
_AI_LABELS: dict[str, str] = {
    "00": "SSCC palette",
    "01": "GTIN colis",
    "02": "GTIN contenu",
    "10": "Lot",
    "11": "Date production",
    "13": "Date emballage",
    "15": "DDM",
    "17": "Date expiration",
    "21": "N° de série",
    "30": "Quantité",
    "37": "Nombre d'unités",
    "240": "Référence supplémentaire",
    "241": "Référence client",
}


def _build_gs1_summary(ais: dict[str, str]) -> str:
    """Phrase courte décrivant le contenu GS1 (« SSCC + GTIN + DDM + Lot »)."""
    if not ais:
        return "GS1-128 vide"
    parts = []
    if "00" in ais:
        parts.append(f"SSCC {_fmt_sscc(ais['00'])}")
    if "01" in ais:
        parts.append(f"GTIN {ais['01']}")
    if "02" in ais:
        parts.append(f"GTIN contenu {ais['02']}")
    if "15" in ais:
        parts.append(f"DDM {ais['15']}")
    if "10" in ais:
        parts.append(f"Lot {ais['10']}")
    if "37" in ais:
        parts.append(f"{ais['37']} unités")
    if not parts:
        parts = [f"{ai}={val}" for ai, val in ais.items()]
    return " · ".join(parts)
