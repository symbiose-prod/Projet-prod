"""
common/easybeer/production_writes.py
====================================
Endpoints d'écriture EB pour la production : Conditionner, Mesures, Terminer
brassin, Sortie stock.

Ces fonctions sont les appels HTTP "bruts" vers Easybeer. Elles sont
appelées par le worker outbox (cf. common/outbox/handlers.py) — pas
directement par les pages NiceGUI ni par l'app iOS, qui utilisent les
wrappers ``enqueue_*`` de ``common/easybeer/queued.py``.

Convention de signatures :
- Reçoivent un dict ``payload`` au format attendu par EB (cf. swagger)
- Retournent la réponse EB (dict) ou propagent l'exception en cas d'échec
  (le worker outbox décide retry/dead-letter)

Endpoints couverts :
- POST /brassin/mise-en-bouteille  (Conditionner)
- POST /brassin/mesure/enregistrer (Mesures + Incidents via nonConformite)
- POST /brassin/terminer            (Terminer + Archiver via archive: true)
- POST /stock/sortie/enregistrer    (Sortie stock = ramasse SOFRIPA)
"""
from __future__ import annotations

from typing import Any

from ._client import _log, retry_api
from .endpoint import execute_endpoint

# ─── Conditionner (= Mise en bouteille) ──────────────────────────────────


@retry_api
def conditionner_brassin(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrapper transport pour ``POST /brassin/mise-en-bouteille``.

    Cette fonction est **un wrapper HTTP fin**. L'orchestration (résolution
    idStockBouteille via templates DB + appel deduction-stocks préalable)
    se fait côté domaine dans
    ``common.services.mise_en_bouteille_orchestrator.execute_mise_en_bouteille``,
    qui est ce que l'handler outbox appelle réellement.

    Cette fonction reste disponible pour push direct d'un payload complet
    (ex. depuis un test d'intégration ou un script ad-hoc). Le payload doit
    déjà contenir tous les champs requis par EB (cf. ``docs/easybeer-write-payloads/
    mise-en-bouteille.request.json``).

    Body attendu (ModeleStockProduit, cf. swagger v2.3.0) :

    - dateMiseEnBouteille (ISO)
    - dateLimiteUtilisationOptimale (ISO) — DDM
    - numeroLot (str)
    - volumeRestant (float)
    - modelesStockProduitBouteille (list, arbre entrepot/fils)
    - modelesStocksMiseEnBouteille (list, déjà calculé via deduction-stocks)
    - modeleBrassin (dict — ModeleBrassin complet, pas ``{id: X}``)
    - modeleElevage (dict, ``{}`` si pas d'élevage)
    - produitsDerives (list)

    Effet côté EB : génère les entrées stock produit fini + déduit MP du stock.
    Réponse typique : ``{"message": "", "map": {}}`` ou body vide.
    """
    result = execute_endpoint(
        method="POST",
        path="brassin/mise-en-bouteille",
        payload=payload,
        allow_empty_2xx=True,  # EB peut renvoyer body vide sur succès
    )
    # Invalider les caches DB pour que les prochaines lectures reflètent le
    # nouveau stock produit et la mise à jour du brassin.
    _invalidate_caches_after_production_write(
        ("brassins_en_cours", "brassins_planifies", "stocks_produits", "autonomie_stocks"),
    )
    return result


# ─── Mesures + Incidents ─────────────────────────────────────────────────


@retry_api
def enregistrer_mesure_brassin(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /brassin/mesure/enregistrer → Enregistre une mesure (avec ou sans incident).

    Body attendu (ModeleBrassinMesure, cf. swagger v2.3.0) :
    - idBrassin (int) — référence brassin
    - idBrassinMesure (int|None) — pour update, sinon None = create
    - etape (str) — fermentation, garde, etc.
    - auteur (str)
    - date (timestamp ms) + dateFormulaire (datetime ISO)
    - densite, ph, temperature, degreAlcool, acidite, pression,
      saturation, pertes (float)
    - qpcr (str) — analyses microbio
    - commentaire (str)
    - **nonConformite (str)** — si rempli = incident (clé pour traçabilité)
    - uniteDensite (dict)

    Effet côté EB : ajoute une ligne dans l'historique des mesures du brassin.
    Un incident est une mesure avec ``nonConformite`` rempli (pas d'endpoint
    incident séparé — confirmé via audit swagger).
    """
    return execute_endpoint(
        method="POST",
        path="brassin/mesure/enregistrer",
        payload=payload,
    )


# ─── Terminer (+ Archiver en option) ─────────────────────────────────────


@retry_api
def terminer_brassin(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /brassin/terminer → Marque le brassin comme terminé (+ archive si demandé).

    Deux modes d'appel :

    **Mode "full"** : le payload est déjà un ModeleBrassin complet (60+ champs).
    Indiqué par ``payload.pop("_full") == True``. On push tel quel.

    **Mode "lazy"** (par défaut) : le payload contient juste ``idBrassin`` +
    quelques overrides (``dateFin``, ``archive``, ``commentaire``, etc.).
    La fonction :
    1. Charge le ModeleBrassin complet via ``get_brassin_detail(idBrassin)``
    2. Applique les overrides au-dessus
    3. Push le résultat à EB

    Le mode lazy est préféré pour les events enqueue via outbox parce que :
    - Le payload outbox reste léger (pas 60+ champs à sérialiser en JSON)
    - On évite les conflits avec des modifs concurrentes côté EB entre
      l'enqueue et le push (retry-safe)
    - Le brassin EB est forcément à jour au moment du push

    Champs notables d'override (cf. swagger ModeleBrassin et payload de
    référence ``docs/easybeer-write-payloads/terminer.request.json``) :
    - ``archive`` (bool) : si True, termine ET archive en une opération
    - ``commentaire`` (str) : HTML, peut contenir le récap
    - ``densiteInitiale``, ``densiteFinale``, ``ph`` (float)
    - ``volumeFinal`` (float)
    - ``dateFinFormulaire`` (str ISO)

    **Compatibilité** : accepte aussi bien ``payload["idBrassin"]`` (nouveau,
    payload conforme à EB) que ``payload["id"]`` (ancien format, pour les
    events en queue avant la migration de naming). ``idBrassin`` a priorité.

    Effet côté EB : passe le brassin en état "terminé" (et "archivé" si flag).
    Réponse EB sur succès : body vide (HTTP 200) — d'où ``allow_empty_2xx``.
    """
    # Mode "full" : payload est déjà complet, on push tel quel
    if payload.pop("_full", False):
        full_payload = payload
    else:
        # Mode "lazy" : on charge le brassin EB complet et on applique les overrides
        from .brassins import get_brassin_detail

        # Accept both "idBrassin" (nouveau, conforme EB) et "id" (legacy events)
        brassin_id = payload.get("idBrassin") or payload.get("id")
        if not brassin_id:
            raise ValueError(
                "terminer_brassin (lazy mode) requires payload['idBrassin']",
            )
        brassin_full = get_brassin_detail(int(brassin_id))
        if not brassin_full:
            raise ValueError(
                f"terminer_brassin: brassin id={brassin_id} introuvable dans EB",
            )
        # Normalize : enlève "id" (qui n'existe pas dans ModeleBrassin EB) et
        # garantit "idBrassin" au top-level (cf. payload de référence EB UI).
        overrides = {k: v for k, v in payload.items() if k != "id"}
        overrides["idBrassin"] = int(brassin_id)
        # Merge : full d'abord, overrides écrasent (sans modifier le cache shared)
        full_payload = {**brassin_full, **overrides}
        _log.debug(
            "terminer_brassin lazy: brassin idBrassin=%s overrides=%s",
            brassin_id, list(overrides.keys()),
        )

    result = execute_endpoint(
        method="POST",
        path="brassin/terminer",
        payload=full_payload,
        # EB confirme le succès par un HTTP 200 + body vide (cf.
        # docs/easybeer-write-payloads/terminer.response.json)
        allow_empty_2xx=True,
    )
    # Le brassin n'est plus dans les listes "en cours" ni "planifiés"
    _invalidate_caches_after_production_write(
        ("brassins_en_cours", "brassins_planifies")
    )
    return result


# ─── Sortie stock (ramasse SOFRIPA) ──────────────────────────────────────


@retry_api
def enregistrer_sortie_stock(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /stock/sortie/enregistrer → Déclare une sortie de stock vers un client.

    Body attendu (ModeleStockSortieForm, cf. swagger v2.3.0) :
    - idClient (int) — destinataire (SOFRIPA dans notre cas)
    - idEntrepot (int) — entrepôt de départ
    - idProduit (int) — produit sortant
    - identifiantLot (str) — N° de lot
    - quantite (float)
    - date (timestamp/str)
    - typeMouvement (int) — type sortie (vente, perte, etc. — cf. /referentiel/commande/type-mouvement)
    - commentaire (str)

    Effet côté EB : décrémente le stock du produit chez le client/entrepôt cible.

    Note : c'est l'endpoint clé pour automatiser la déclaration ramasse SOFRIPA
    actuellement faite manuellement par le responsable d'atelier.
    """
    result = execute_endpoint(
        method="POST",
        path="stock/sortie/enregistrer",
        payload=payload,
    )
    # Sortie = stock impacté → invalider les caches stock
    _invalidate_caches_after_production_write(
        ("stocks_produits", "autonomie_stocks")
    )
    return result


# ─── Helper interne ──────────────────────────────────────────────────────


def _invalidate_caches_after_production_write(cache_keys: tuple[str, ...]) -> None:
    """Invalide plusieurs caches L2 DB après une écriture EB.

    Best-effort : si l'invalidation échoue (DB down, etc.) on log et on continue,
    pas question de faire échouer le worker outbox pour ça.
    """
    try:
        from common._session import current_tenant_id
        from common.eb_cache import cache_delete
        tid = current_tenant_id()
        if not tid:
            return
        for key in cache_keys:
            try:
                cache_delete(tid, key)
            except Exception:
                _log.debug("Cache invalidation failed for %s", key, exc_info=True)
    except Exception:
        _log.debug("Cache invalidation skipped (no tenant context)", exc_info=True)
