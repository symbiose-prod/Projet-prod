"""
common/services/ramasse_service.py
==================================
Service domaine : orchestration des données EasyBeer nécessaires au remplissage
d'une fiche de ramasse (brassins + matrices + poids + entrepôt).

Pattern : chaque appel EasyBeer est isolé dans sa propre fonction qui absorbe
les erreurs transport (EasyBeerError, RequestException) et log en warning.
Les appelants reçoivent ``None`` ou une liste vide plutôt qu'une exception —
la page ramasse peut ainsi se rendre en mode dégradé (sans codes-barres, sans
poids, etc.) au lieu de crasher si un endpoint EB est indisponible.

La fonction agrégée :func:`load_initial_data` lance les 4 fetchs en parallèle
via un ThreadPoolExecutor (gain typique : ~1.5 s → ~0.5 s sur ouverture page
ramasse).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date as _date
from typing import Any

import requests

from common.easybeer import (
    EasyBeerError,
    fetch_carton_weights,
    get_brassins_archives,
    get_brassins_en_cours,
    get_code_barre_matrice,
    get_warehouses,
)
from common.ramasse import parse_barcode_matrix

_log = logging.getLogger("ferment.services.ramasse")


@dataclass(frozen=True)
class RamasseInitialData:
    """Paquet de données EasyBeer renvoyé par :func:`load_initial_data`.

    Tous les champs sont optionnels — si un fetch EB échoue, on renvoie None
    ou [] pour ne pas bloquer l'ouverture de la page ramasse.

    Attributes:
        brassins: Liste brassins en cours + archives non annulés (archive
            marquée par ``_is_archive=True``).
        brassin_load_errors: Messages d'erreur utilisateur à afficher en
            bandeau de la page si la liste est incomplète.
        cb_by_product: Matrice codes-barres indexée par idProduit, ou
            ``None`` si l'endpoint EB n'a pas répondu.
        id_entrepot: ID de l'entrepôt principal (flag ``principal=true``),
            fallback sur le premier entrepôt de la liste.
        eb_weights: Mapping ``(idProduit, format_code)`` → poids unitaire (kg).
    """
    brassins: list[dict]
    brassin_load_errors: list[str]
    cb_by_product: dict[int, list[dict]] | None
    id_entrepot: int | None
    eb_weights: dict[tuple[int, str], float] | None


def load_active_brassins(nb_archives: int = 3) -> tuple[list[dict], list[str]]:
    """Charge brassins en cours + N derniers archivés (dédupliqués).

    Les brassins archivés qui ne sont pas déjà dans la liste "en cours" sont
    ajoutés avec le flag ``_is_archive=True`` (pour différenciation UI). Les
    brassins annulés (``annule=True``) sont filtrés.

    Returns:
        (liste des brassins, liste des messages d'erreur rencontrés)
    """
    errors: list[str] = []

    try:
        en_cours = get_brassins_en_cours()
    except (EasyBeerError, requests.RequestException) as exc:
        errors.append(f"Brassins en cours : {exc}")
        en_cours = []

    en_cours_ids = {b.get("idBrassin") for b in en_cours}
    try:
        archives = get_brassins_archives(nombre=nb_archives)
        for b in archives:
            if b.get("idBrassin") not in en_cours_ids:
                b["_is_archive"] = True
                en_cours.append(b)
    except (EasyBeerError, requests.RequestException) as exc:
        errors.append(f"Brassins archivés : {exc}")
    return [b for b in en_cours if not b.get("annule")], errors


def load_barcode_matrix() -> dict[int, list[dict]] | None:
    """Charge la matrice codes-barres EasyBeer et la parse par produit.

    Returns:
        ``{idProduit: [{code, format, ...}]}`` ou ``None`` si l'appel échoue.
    """
    try:
        raw = get_code_barre_matrice()
        return parse_barcode_matrix(raw)
    except (EasyBeerError, requests.RequestException):
        _log.warning("Impossible de charger la matrice codes-barres", exc_info=True)
        return None


def load_carton_weights() -> dict[tuple[int, str], float] | None:
    """Charge les poids unitaires cartons depuis EasyBeer (cache fichier).

    Returns:
        ``{(idProduit, format_code): poids_kg}`` ou ``None`` en cas d'échec.
    """
    try:
        return fetch_carton_weights()
    except (EasyBeerError, requests.RequestException):
        _log.warning("Impossible de charger les poids cartons", exc_info=True)
        return None


def load_main_entrepot_id() -> int | None:
    """Retourne l'idEntrepot de l'entrepôt principal (flag ``principal=true``).

    Fallback sur le premier entrepôt de la liste si aucun n'est marqué
    principal. Retourne ``None`` si l'endpoint échoue ou si la liste est vide.
    """
    try:
        warehouses = get_warehouses()
        for w in warehouses:
            if w.get("principal"):
                return w.get("idEntrepot")
        return warehouses[0].get("idEntrepot") if warehouses else None
    except (EasyBeerError, requests.RequestException):
        _log.warning("Impossible de charger les entrepots", exc_info=True)
        return None


def load_initial_data(*, nb_archives: int = 3) -> RamasseInitialData:
    """Fetch parallèle de toutes les données EasyBeer nécessaires à la page ramasse.

    Orchestration via ThreadPoolExecutor(max_workers=4) — les 4 endpoints
    EasyBeer sont I/O-bound et indépendants. Bloquant ; à appeler depuis
    ``asyncio.to_thread(load_initial_data)`` côté page NiceGUI pour ne pas
    bloquer l'event loop.

    Args:
        nb_archives: Nombre de brassins archivés à charger (défaut 3).
    """
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_brassins = pool.submit(load_active_brassins, nb_archives)
        f_cb = pool.submit(load_barcode_matrix)
        f_entrepot = pool.submit(load_main_entrepot_id)
        f_weights = pool.submit(load_carton_weights)

    brassins, errors = f_brassins.result()
    return RamasseInitialData(
        brassins=brassins,
        brassin_load_errors=errors,
        cb_by_product=f_cb.result(),
        id_entrepot=f_entrepot.result(),
        eb_weights=f_weights.result(),
    )


# ─── Helpers de composition de l'envoi (pures, testables) ────────────────────

@dataclass(frozen=True)
class RamasseTotals:
    """Totaux agrégés d'une liste de lignes actives (cartons > 0)."""
    cartons: int
    palettes: int
    poids_kg: int


def compute_totals(active_rows: list[dict[str, Any]]) -> RamasseTotals:
    """Somme cartons / palettes / poids des lignes actives.

    Chaque ligne peut avoir des valeurs ``None`` ou des strings numériques
    (source : binding Quasar Table) — on coerce en int défensivement.
    """
    tot_cartons = sum(int(r.get("cartons") or 0) for r in active_rows)
    tot_palettes = sum(int(r.get("palettes") or 0) for r in active_rows)
    tot_poids = sum(int(r.get("poids") or 0) for r in active_rows)
    return RamasseTotals(
        cartons=tot_cartons,
        palettes=tot_palettes,
        poids_kg=tot_poids,
    )


def build_lines_payload(active_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sérialise les lignes pour :func:`common.ramasse_history.save_ramasse`.

    Retient uniquement les colonnes persistées (ref, produit, ddm, cartons,
    palettes, poids). Les valeurs sont normalisées en int pour les numériques.
    """
    return [
        {
            "ref": r.get("ref", ""),
            "produit": r.get("produit", ""),
            "ddm": r.get("ddm", ""),
            "cartons": int(r.get("cartons") or 0),
            "palettes": int(r.get("palettes") or 0),
            "poids": int(r.get("poids") or 0),
        }
        for r in active_rows
    ]


def build_email_subject(
    date_ramasse: _date,
    *,
    is_update: bool = False,
    version: int = 1,
) -> str:
    """Construit le sujet de l'email ramasse (v1 ou mise à jour v2+)."""
    if is_update:
        return (
            f"Mise à jour de la ramasse du {date_ramasse:%d/%m/%Y} "
            f"— Ferment Station (v{version})"
        )
    return f"Demande de ramasse — {date_ramasse:%d/%m/%Y} — Ferment Station"


def _render_packaging_block(packaging_lines: list[dict] | None) -> str:
    """Bloc HTML liste des emballages à ramener. Vide si aucun."""
    if not packaging_lines:
        return ""
    items = "<br>".join(
        f"— {p.get('qty', 0)} {p.get('unit', '')}(s) {p.get('label', '')}"
        for p in packaging_lines
    )
    return f"<p><strong>Emballages à ramener :</strong><br>{items}</p>"


_SIGNATURE_HTML = (
    "<hr>\n"
    "<p><strong>Ferment Station</strong><br>\n"
    "Producteur de boissons fermentées<br>\n"
    "26 Rue Robert Witchitz – 94200 Ivry-sur-Seine</p>"
)


def build_email_body(
    date_ramasse: _date,
    *,
    total_palettes: int,
    total_cartons: int,
    packaging_lines: list[dict] | None = None,
    is_update: bool = False,
    version: int = 1,
) -> str:
    """Corps HTML de l'email de ramasse.

    Deux modes :
    - v1 (création) : message court demandant la ramasse.
    - v2+ (mise à jour) : mention explicite du remplacement, pointe vers le
      PDF différentiel (jaune = ajouts, bleu = modifications).

    La signature et l'adresse sont toujours ajoutées en pied de mail.
    """
    pkg_html = _render_packaging_block(packaging_lines)
    plural_pal = "s" if total_palettes != 1 else ""

    if is_update:
        body = f"""
        <p>Bonjour,</p>
        <p>Nous vous envoyons une <strong>mise à jour</strong> de la ramasse
        prévue pour le <strong>{date_ramasse:%d/%m/%Y}</strong> (version {version}).</p>
        <p>Nouveau total : <strong>{total_palettes}</strong>
        palette{plural_pal} ({total_cartons} cartons).</p>
        <p>Merci de bien vouloir tenir compte de cette version qui
        <strong>remplace</strong> la précédente. Le PDF ci-joint fait
        apparaître les changements :
        <strong>nouvelles lignes en jaune</strong>,
        <strong>lignes modifiées en bleu</strong>
        (avec l'ancien nombre de cartons indiqué).</p>
        {pkg_html}
        <p>Merci pour votre compréhension,<br>Bonne journée.</p>
        {_SIGNATURE_HTML}
        """
    else:
        body = f"""
        <p>Bonjour,</p>
        <p>Nous aurions besoin d'une ramasse pour le {date_ramasse:%d/%m/%Y}.<br>
        Pour <strong>{total_palettes}</strong> palette{plural_pal}.</p>
        {pkg_html}
        <p>Merci,<br>Bon après-midi.</p>
        {_SIGNATURE_HTML}
        """
    return body
