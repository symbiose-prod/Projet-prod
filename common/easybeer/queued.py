"""
common/easybeer/queued.py
=========================
Wrappers ``enqueue_*`` pour pousser des écritures EB via l'outbox (async),
sans bloquer l'utilisateur sur la latence réseau et avec garantie de
retry transparent en cas d'échec ponctuel.

Pattern d'utilisation côté caller :

    from common.easybeer.queued import enqueue_brassin_creation
    enqueue_brassin_creation(
        tenant_id=tid,
        payload={"nom": "...", ...},
        user_email=current_user_email,
    )

Le caller obtient immédiatement le retour (id de l'event outbox) sans attendre
EB. Le worker async se chargera de l'appel HTTP réel et retentera si échec.

⚠️ Ces wrappers ne retournent PAS la réponse d'EB (id du brassin créé, etc.)
parce que l'appel est asynchrone. Pour les cas où on a besoin de la réponse
immédiate (ex: afficher l'id généré côté EB), continuer d'utiliser les
fonctions directes dans common/easybeer/brassins.py + conditioning.py.

Au Sprint 2 et au-delà, tous les nouveaux writes EB doivent passer par ces
wrappers — c'est le pattern par défaut.
"""
from __future__ import annotations

import logging
from typing import Any

from common.outbox import enqueue_event

_log = logging.getLogger("ferment.easybeer.queued")


def enqueue_brassin_creation(
    *,
    tenant_id: str,
    payload: dict[str, Any],
    user_email: str | None = None,
) -> int | None:
    """Enqueue un POST /brassin/enregistrer.

    payload : body attendu par EB (cf. swagger ModeleBrassin)
    """
    return enqueue_event(
        tenant_id=tenant_id,
        event_type="brassin.create",
        payload=payload,
        created_by=user_email,
    )


def enqueue_planification_conditionnement_add(
    *,
    tenant_id: str,
    payload: dict[str, Any],
    user_email: str | None = None,
) -> int | None:
    """Enqueue un POST /brassin/planification-conditionnement/ajouter."""
    return enqueue_event(
        tenant_id=tenant_id,
        event_type="brassin.planification.add",
        payload=payload,
        created_by=user_email,
    )


def enqueue_planification_conditionnement_delete(
    *,
    tenant_id: str,
    id_planification: int,
    user_email: str | None = None,
) -> int | None:
    """Enqueue un GET /brassin/planification-conditionnement/supprimer/{id}."""
    return enqueue_event(
        tenant_id=tenant_id,
        event_type="brassin.planification.delete",
        payload={"id": id_planification},
        created_by=user_email,
    )


# Sprint 2 ajoutera les wrappers suivants :
#
# def enqueue_brassin_mise_en_bouteille(...) → "brassin.mise-en-bouteille"
# def enqueue_brassin_mesure(...) → "brassin.mesure"
# def enqueue_brassin_terminer(...) → "brassin.terminer"
# def enqueue_stock_sortie(...) → "stock.sortie"
# def enqueue_douane_dae_export(...) → "douane.dae.export"
