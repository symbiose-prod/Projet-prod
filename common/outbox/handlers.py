"""
common/outbox/handlers.py
=========================
Dispatcher event_type → callable EB.

Chaque event_type listé dans EVENT_HANDLERS correspond à une fonction qui
fait l'appel HTTP réel vers Easybeer. Le worker async lit l'event de l'outbox
et appelle le handler approprié.

Pour ajouter un nouvel event :
1. Créer (ou réutiliser) la fonction d'écriture EB dans common/easybeer/*
2. Ajouter une entrée dans EVENT_HANDLERS avec le bon mapping payload → fonction
3. Documenter le format de payload attendu dans la docstring du handler

Convention event_type : "<domaine>.<action>" en kebab-case
  ex: "brassin.create", "brassin.terminer", "brassin.mise-en-bouteille",
      "brassin.mesure", "stock.sortie", "douane.dae.export"
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

_log = logging.getLogger("ferment.outbox.handlers")


# ─── Handlers — un par event_type ─────────────────────────────────────────


def _handle_brassin_create(payload: dict[str, Any]) -> dict[str, Any]:
    """event_type='brassin.create' → POST /brassin/enregistrer."""
    from common.easybeer.brassins import create_brassin
    return create_brassin(payload)


def _handle_brassin_planification_add(payload: dict[str, Any]) -> dict[str, Any]:
    """event_type='brassin.planification.add' → POST /brassin/planification-conditionnement/ajouter."""
    from common.easybeer.conditioning import add_planification_conditionnement
    return add_planification_conditionnement(payload)


def _handle_brassin_planification_delete(payload: dict[str, Any]) -> dict[str, Any]:
    """event_type='brassin.planification.delete' → GET /brassin/planification-conditionnement/supprimer/{id}.

    Payload attendu : {"id": int}

    Note : la fonction sous-jacente retourne None ; on retourne {} pour
    rester conforme à la signature attendue par le dispatcher.
    """
    from common.easybeer.brassins import delete_conditioning_line
    delete_conditioning_line(int(payload["id"]))
    return {}


# Note : upload_fichier_brassin (POST /brassin/upload/{id}) prend des bytes
# binaires en multipart-form-data — incompatible avec une persistance JSON
# propre dans l'outbox (encoding base64 alourdirait inutilement la queue pour
# un cas peu critique). On le laisse en appel direct, hors outbox.


# ─── Sprint 2 : Conditionner, Mesures, Terminer, Sortie stock ────────────


def _handle_brassin_mise_en_bouteille(payload: dict[str, Any]) -> dict[str, Any]:
    """event_type='brassin.mise-en-bouteille' → orchestrer 2 calls EB.

    Pipeline (cf. ``common.services.mise_en_bouteille_orchestrator``) :

    1. ``get_brassin_detail(idBrassin)`` → brassin complet
    2. ``resolve_bottle_stock`` pour chaque item → idStockBouteille
    3. ``POST /brassin/deduction-stocks-conditionnement`` → BOM
    4. ``POST /brassin/mise-en-bouteille`` → crée le stock produit fini
    """
    from common.services.mise_en_bouteille_orchestrator import (
        execute_mise_en_bouteille,
    )
    return execute_mise_en_bouteille(payload)


def _handle_brassin_mesure(payload: dict[str, Any]) -> dict[str, Any]:
    """event_type='brassin.mesure' → POST /brassin/mesure/enregistrer.

    Enregistre une mesure (densité, T°, pH, etc.) ou un incident (champ
    ``nonConformite`` rempli).
    """
    from common.easybeer.production_writes import enregistrer_mesure_brassin
    return enregistrer_mesure_brassin(payload)


def _handle_brassin_terminer(payload: dict[str, Any]) -> dict[str, Any]:
    """event_type='brassin.terminer' → POST /brassin/terminer.

    Marque le brassin comme terminé (et archivé si ``archive: True`` dans le
    payload).
    """
    from common.easybeer.production_writes import terminer_brassin
    return terminer_brassin(payload)


def _handle_stock_sortie(payload: dict[str, Any]) -> dict[str, Any]:
    """event_type='stock.sortie' → POST /stock/sortie/enregistrer.

    Déclare une sortie de stock vers un client (ex: ramasse SOFRIPA).
    """
    from common.easybeer.production_writes import enregistrer_sortie_stock
    return enregistrer_sortie_stock(payload)


# ─── Dispatcher ──────────────────────────────────────────────────────────

EVENT_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    # Sprint 1 — writes existants migrés
    "brassin.create": _handle_brassin_create,
    "brassin.planification.add": _handle_brassin_planification_add,
    "brassin.planification.delete": _handle_brassin_planification_delete,
    # Sprint 2 — élimination de la double saisie manuelle
    "brassin.mise-en-bouteille": _handle_brassin_mise_en_bouteille,
    "brassin.mesure": _handle_brassin_mesure,
    "brassin.terminer": _handle_brassin_terminer,
    "stock.sortie": _handle_stock_sortie,
}


class UnknownEventType(Exception):
    """Levée si un event_type inconnu est rencontré (event créé par une
    version plus récente du code, ou nom mal orthographié)."""


def dispatch(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Appelle le handler EB approprié pour cet event.

    Lève UnknownEventType si event_type n'est pas enregistré, ou propage
    l'exception du handler (typiquement EasyBeerError, HTTPError) — c'est le
    worker qui décide de retry/dead-letter selon la nature de l'erreur.
    """
    handler = EVENT_HANDLERS.get(event_type)
    if handler is None:
        raise UnknownEventType(f"No handler for event_type={event_type!r}")
    _log.debug("Dispatching event_type=%s", event_type)
    return handler(payload)
