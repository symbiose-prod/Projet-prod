"""
common/services/bottle_stock_resolver.py
=========================================
Résolveur **(produit, format, marque) → idStockBouteille** pour la
construction du payload ``POST /brassin/mise-en-bouteille``.

**Problème** : pour mettre en bouteille un brassin, EB attend dans le
payload, pour chaque format conditionné, le ``idStockBouteille`` (le stock
des bouteilles vides à débiter). Le brassin EB expose les stocks bouteille
disponibles (``brassin.modelesStockProduitBouteille[0].modelesFils[]``)
mais peut en avoir plusieurs pour une même contenance (ex. 75cl Verralia
vs 75cl SAFT). Il faut désambiguïser.

**Approche** : on s'appuie sur la table ``eb_stock_product_templates``
(populée par ``common/easybeer/stock_templates_sync.py``) qui, pour
chaque codeArticle EB (ex. ``SK-KDF-75-ORI``), donne le
``contenant_libelle`` exact (ex. ``"Bouteille 75cl Verralia - 0.75L"``).
On matche ce libellé contre celui des fils du brassin pour retrouver le
bon ``idStockBouteille``.

**Cascade de résolution** (best-effort, le plus précis d'abord) :

1. Lookup unique : ``(id_produit, contenance, lot_quantite)`` dans la
   table renvoie 1 seul template → match direct.
2. Lookup multiple (75cl Verralia + 75cl SAFT) : on filtre par mot-clé
   issu de ``marque`` ou ``fmt`` (ex. ``"NIKO"``, ``"SAFT"``, ``"4x"``).
3. Si ambiguïté persiste : ``None`` + log explicite → l'event outbox va
   en dead-letter avec un message actionnable pour l'opérateur.

Cf. ``docs/easybeer-write-payloads/`` pour les payloads de référence.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from db.conn import run_sql

_log = logging.getLogger("ferment.bottle_stock_resolver")


@dataclass(frozen=True)
class BottleStockResolution:
    """Résultat d'une résolution réussie pour un item de conditionnement."""
    id_stock_bouteille: int
    contenant_libelle: str
    contenance: float
    code_article: str
    id_stock_produit: int
    lot_quantite: int                # PCB (ex 6 pour Carton de 6)
    id_lot: int | None               # idLot EB (ex 3 = "Carton de 6"), requis dans payload mise-en-bouteille
    elements_conditionnement: list[dict[str, Any]]


# ─── Helpers ──────────────────────────────────────────────────────────────


_FMT_RE = re.compile(r"^\s*(\d+)\s*x\s*(\d+)\s*$", re.IGNORECASE)


def parse_fmt(fmt: str) -> tuple[int, float] | None:
    """Parse un fmt iOS type ``"6x33"`` / ``"12x33"`` / ``"4x75"`` → ``(pcb, contenance_l)``.

    Retourne None si parsing échoue (ex. fmt vide, format inconnu).

    Examples:
        >>> parse_fmt("6x33")
        (6, 0.33)
        >>> parse_fmt("12x33")
        (12, 0.33)
        >>> parse_fmt("4x75")
        (4, 0.75)
        >>> parse_fmt("") is None
        True
    """
    if not fmt:
        return None
    m = _FMT_RE.match(fmt)
    if not m:
        return None
    try:
        pcb = int(m.group(1))
        cl = int(m.group(2))
        if pcb <= 0 or cl <= 0:
            return None
        return (pcb, cl / 100.0)
    except (ValueError, TypeError):
        return None


def _find_templates_by_contenance(
    *,
    tenant_id: str,
    id_produit: int,
    contenance: float,
    lot_quantite: int,
) -> list[dict[str, Any]]:
    """Cherche tous les templates matching (produit, contenance, PCB).

    Le ``find_template`` simple retourne None si plusieurs matches. Ici on
    veut la liste pour pouvoir désambiguïser par marque/fmt.
    """
    rows = run_sql(
        """
        SELECT id_stock_produit, code_article, id_produit, produit_libelle,
               id_contenant, contenant_libelle, contenance,
               id_lot, lot_libelle, lot_quantite,
               elements_conditionnement
          FROM eb_stock_product_templates
         WHERE tenant_id    = :tid
           AND id_produit   = :ip
           AND ABS(contenance - :ct) < 0.001
           AND lot_quantite = :lq
         ORDER BY code_article
        """,
        {"tid": tenant_id, "ip": id_produit, "ct": contenance, "lq": lot_quantite},
    ) or []
    # `id_lot` est déjà dans le SELECT — pas de modif nécessaire.
    return [dict(r) for r in rows]


def _disambiguate_by_marque_or_fmt(
    templates: list[dict[str, Any]],
    *,
    marque: str,
    fmt: str,
) -> dict[str, Any] | None:
    """Cas 2 templates matchent (ex 75cl Verralia + 75cl SAFT) : tranche par
    mot-clé.

    Règles heuristiques basées sur les conventions EB observées :
    - Brassin pour la marque ``NIKO`` → bouteille SAFT (codeArticle commence
      par ``NIKO-``)
    - Pack de 4 (fmt commence par ``4x``) Symbiose → bouteille SAFT
    - Sinon (Symbiose Carton de N) → Verralia (libellé contient
      ``"Verralia"`` ou ``"EAU GAZEUSE"`` pour l'ancien naming)

    Retourne ``None`` si toujours ambigu.
    """
    if len(templates) == 1:
        return templates[0]
    if not templates:
        return None

    marque_norm = (marque or "").upper().strip()
    fmt_norm = (fmt or "").lower().strip()

    # Règle 1 : NIKO → cherche un codeArticle qui commence par NIKO-
    if marque_norm == "NIKO":
        niko = [t for t in templates if t["code_article"].startswith("NIKO-")]
        if len(niko) == 1:
            return niko[0]
        _log.warning(
            "disambiguate: marque=NIKO, %d templates NIKO-* trouvés (%s) — ambigu",
            len(niko),
            [t["code_article"] for t in niko],
        )
        return None

    # Règle 2 : Pack de 4 → contenant libellé "SAFT"
    if fmt_norm.startswith("4x"):
        saft = [t for t in templates if "saft" in (t["contenant_libelle"] or "").lower()]
        if len(saft) == 1:
            return saft[0]
        _log.warning(
            "disambiguate: fmt=4x, %d templates SAFT (%s) — ambigu",
            len(saft),
            [t["code_article"] for t in saft],
        )
        return None

    # Règle 3 : Symbiose Carton de N (6x, 12x) → Verralia (par défaut)
    verralia = [
        t for t in templates
        if "verralia" in (t["contenant_libelle"] or "").lower()
        or "eau gazeuse" in (t["contenant_libelle"] or "").lower()
        or t["code_article"].startswith("SK-")
    ]
    if len(verralia) == 1:
        return verralia[0]
    # Si encore ambigu, on prend le 1er SK-* (Symbiose) comme fallback
    sk_templates = [t for t in templates if t["code_article"].startswith("SK-")]
    if len(sk_templates) == 1:
        return sk_templates[0]

    _log.warning(
        "disambiguate: marque=%s fmt=%s, %d templates — ambigu : %s",
        marque, fmt, len(templates),
        [t["code_article"] for t in templates],
    )
    return None


def _match_fils_by_contenant_libelle(
    brassin_fils: list[dict[str, Any]],
    *,
    contenant_libelle: str,
    contenance: float,
) -> dict[str, Any] | None:
    """Trouve le fil du brassin dont le libellé correspond au contenant cible.

    1. Match exact sur ``libelle``
    2. Match par contenance + keyword (Verralia/SAFT) dans le libellé
    3. Si une seule bouteille pour cette contenance : la prendre
    """
    cl_lower = (contenant_libelle or "").lower()
    eligible = [
        f for f in brassin_fils
        if abs(float(f.get("contenance") or 0) - contenance) < 0.001
    ]
    if not eligible:
        return None

    # 1. Match exact (insensible à la casse)
    for fil in eligible:
        if (fil.get("libelle") or "").lower() == cl_lower:
            return fil

    # 2. Match par keyword
    keywords = []
    if "verralia" in cl_lower:
        keywords = ["verralia", "eau gazeuse"]
    elif "saft" in cl_lower:
        keywords = ["saft"]
    for kw in keywords:
        matches = [f for f in eligible if kw in (f.get("libelle") or "").lower()]
        if len(matches) == 1:
            return matches[0]

    # 3. Si un seul fil de cette contenance, le prendre
    if len(eligible) == 1:
        return eligible[0]

    return None


# ─── API publique ─────────────────────────────────────────────────────────


def resolve_bottle_stock(
    *,
    tenant_id: str,
    brassin_fils: list[dict[str, Any]],
    id_produit: int,
    fmt: str,
    marque: str,
) -> BottleStockResolution | None:
    """Résout ``(id_produit, fmt, marque)`` → ``BottleStockResolution`` ou None.

    Args:
        tenant_id: scope multi-tenant pour les lookups DB.
        brassin_fils: ``brassin.modelesStockProduitBouteille[0].modelesFils[]``
            depuis ``get_brassin_detail(idBrassin)``.
        id_produit: idProduit EB du brassin (ex 42397 pour Kéfir de fruits Original).
        fmt: format depuis la fiche iOS (``"6x33"``, ``"12x33"``, ``"4x75"``).
        marque: marque commerciale depuis la fiche (``"SYMBIOSE"``, ``"NIKO"``).

    Returns:
        ``BottleStockResolution`` si toutes les étapes réussissent, sinon
        ``None`` avec un log warning explicite (caller doit gérer l'échec
        — généralement en faisant échouer l'event outbox vers dead-letter).
    """
    parsed = parse_fmt(fmt)
    if not parsed:
        _log.warning("resolve: fmt invalide '%s'", fmt)
        return None
    pcb, contenance = parsed

    # Étape 1 : trouve le template (codeArticle) qui matche (produit, contenance, PCB)
    templates = _find_templates_by_contenance(
        tenant_id=tenant_id,
        id_produit=id_produit,
        contenance=contenance,
        lot_quantite=pcb,
    )
    if not templates:
        _log.warning(
            "resolve: aucun template pour produit=%s contenance=%s pcb=%s "
            "(table eb_stock_product_templates non sync ou produit inconnu)",
            id_produit, contenance, pcb,
        )
        return None

    template = _disambiguate_by_marque_or_fmt(templates, marque=marque, fmt=fmt)
    if not template:
        return None

    # Étape 2 : trouve le fil du brassin qui matche le contenant du template
    fil = _match_fils_by_contenant_libelle(
        brassin_fils,
        contenant_libelle=template["contenant_libelle"],
        contenance=contenance,
    )
    if not fil:
        _log.warning(
            "resolve: aucun fil brassin matche template codeArticle=%s "
            "contenant='%s' (brassin a %d fils contenance=%s : %s)",
            template["code_article"], template["contenant_libelle"],
            sum(1 for f in brassin_fils if abs(float(f.get("contenance") or 0) - contenance) < 0.001),
            contenance,
            [f.get("libelle") for f in brassin_fils],
        )
        return None

    id_stock_bouteille = fil.get("idStockBouteille")
    if not id_stock_bouteille:
        _log.warning("resolve: fil sans idStockBouteille: %s", fil)
        return None

    return BottleStockResolution(
        id_stock_bouteille=int(id_stock_bouteille),
        contenant_libelle=str(fil.get("libelle") or template["contenant_libelle"]),
        contenance=contenance,
        code_article=template["code_article"],
        id_stock_produit=int(template["id_stock_produit"]),
        lot_quantite=pcb,
        id_lot=int(template["id_lot"]) if template.get("id_lot") is not None else None,
        elements_conditionnement=template.get("elements_conditionnement") or [],
    )
