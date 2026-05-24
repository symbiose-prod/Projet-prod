"""
common/services/mise_en_bouteille_orchestrator.py
==================================================
Orchestre la mise-en-bouteille d'un brassin EB depuis un **payload léger**
généré par ``common.services.production_sheet_eb_bind.build_mise_en_bouteille_payload``.

Cette couche **service** (domaine) coordonne :

- L'accès au brassin EB (transport) via ``common.easybeer.brassins.get_brassin_detail``
- La résolution ``(fmt, marque) → idStockBouteille`` (domaine) via
  ``common.services.bottle_stock_resolver``
- Deux appels HTTP EB (transport) : ``deduction-stocks-conditionnement``
  puis ``mise-en-bouteille``

Pourquoi un service séparé ? Le test d'architecture ``test_easybeer_layers``
interdit que ``common/easybeer/`` (transport) importe ``common/services/``
(domaine). On garde donc l'orchestration ici, et ``production_writes`` ne
contient que des wrappers HTTP fins.

Cf. ``docs/easybeer-write-payloads/`` pour les payloads de référence.
"""
from __future__ import annotations

import logging
from typing import Any

from common.easybeer.brassins import get_brassin_preparation_conditionnement
from common.easybeer.endpoint import execute_endpoint
from common.services.bottle_stock_resolver import resolve_bottle_stock

_log = logging.getLogger("ferment.eb.mise_en_bouteille")


def execute_mise_en_bouteille(payload: dict[str, Any]) -> dict[str, Any]:
    """Orchestre la mise-en-bouteille d'un brassin via 2 appels EB.

    **Payload léger attendu** (généré par ``build_mise_en_bouteille_payload``) :

    .. code-block:: json

        {
            "idBrassin": 259288,
            "tenantId": "uuid-...",
            "numeroLot": "KDF18052026",
            "dateMiseEnBouteille": "2026-05-23T18:04:01.000Z",
            "items": [
                {"marque": "SYMBIOSE", "fmt": "6x33", "cartons": 199},
                {"marque": "SYMBIOSE", "fmt": "6x75", "cartons": 200}
            ],
            "dateLimiteUtilisationOptimale": "2027-05-18T00:00:00.000Z"
        }

    **Pipeline** :

    1. ``get_brassin_detail(idBrassin)`` → ModeleBrassin complet (42 clés)
    2. Pour chaque item : ``resolve_bottle_stock`` → ``idStockBouteille``
       depuis la table ``eb_stock_product_templates``
    3. Construire ``modelesStockProduitBouteille`` : arbre
       ``[{libelle: "FERMENT STATION", modelesFils: [...]}]``
    4. ``POST /brassin/deduction-stocks-conditionnement`` →
       EB calcule ``modelesStocksMiseEnBouteille`` (capsules/cartons/
       étiquettes à débiter)
    5. Inject la BOM calculée dans le payload + champs depuis ``brassin_full``
    6. ``POST /brassin/mise-en-bouteille`` (réponse souvent ``{"message":"","map":{}}``)

    Raises:
        ValueError: si payload mal formé ou si une résolution échoue.
        EasyBeerError: si l'un des appels HTTP échoue.

    Returns:
        Réponse du POST mise-en-bouteille (succès silencieux EB).
    """
    # ── Validation payload léger ─────────────────────────────────────────
    brassin_id = payload.get("idBrassin")
    if not brassin_id:
        raise ValueError(
            "execute_mise_en_bouteille: payload['idBrassin'] manquant",
        )
    tenant_id = payload.get("tenantId") or ""
    if not tenant_id:
        raise ValueError(
            "execute_mise_en_bouteille: payload['tenantId'] manquant",
        )
    items = payload.get("items") or []
    if not items:
        raise ValueError("execute_mise_en_bouteille: payload['items'] vide")
    numero_lot = payload.get("numeroLot") or ""
    date_mise = payload.get("dateMiseEnBouteille")
    if not (numero_lot and date_mise):
        raise ValueError(
            "execute_mise_en_bouteille: numeroLot ou dateMiseEnBouteille manquant",
        )

    # ── 1. Fetch le squelette de payload pré-rempli par EB ───────────────
    # GET /brassin/preparation-conditionnement/brassin/{id} retourne une
    # struct prête à l'emploi avec modeleBrassin complet + modeleElevage +
    # produitsDerives + modelesStockProduitBouteille (avec les fils + leurs
    # idStockBouteille) + volumeRestant + dateLimiteUtilisationOptimale.
    # Cf. docs/easybeer-write-payloads/preparation-conditionnement.response.json
    base_payload = get_brassin_preparation_conditionnement(int(brassin_id))
    if not base_payload:
        raise ValueError(
            f"execute_mise_en_bouteille: brassin idBrassin={brassin_id} "
            "introuvable via preparation-conditionnement",
        )

    # Récupère les fils du brassin (stocks bouteille dispo pour ce produit)
    msb_list = base_payload.get("modelesStockProduitBouteille") or []
    if not msb_list:
        raise ValueError(
            f"execute_mise_en_bouteille: brassin {brassin_id} sans "
            "modelesStockProduitBouteille même via preparation-conditionnement "
            "(produit sans stock bouteille configuré côté EB ?)",
        )
    # Premier élément = entrepôt principal (ex "FERMENT STATION")
    entrepot_root = msb_list[0]
    brassin_fils = entrepot_root.get("modelesFils") or []
    if not brassin_fils:
        raise ValueError(
            f"execute_mise_en_bouteille: brassin {brassin_id} sans fils "
            "(stocks bouteille)",
        )

    id_produit = (base_payload.get("modeleBrassin") or {}).get("produit", {}).get(
        "idProduit",
    )
    if not id_produit:
        # Fallback : produitsDerives[0].idProduit
        produits_derives = base_payload.get("produitsDerives") or []
        if produits_derives:
            id_produit = produits_derives[0].get("idProduit")
    if not id_produit:
        raise ValueError(
            f"execute_mise_en_bouteille: brassin {brassin_id} sans idProduit "
            "détectable (ni dans modeleBrassin.produit ni dans produitsDerives)",
        )

    # ── 2-3. Résoudre chaque item & mettre à jour les fils ──────────────
    # On part des fils tels que EB les a fournis, et on set
    # ``quantiteMiseEnBouteille`` + ``lot.idLot`` + ``dateDisponibilite``
    # sur ceux qu'on utilise. Les fils non utilisés gardent ``quantite=None``
    # mais ont AUSSI besoin d'un ``lot`` (avec idLot=1 par défaut = Pack de 4)
    # pour que EB accepte le payload — cf. HAR de référence.
    DEFAULT_UNUSED_LOT = {"idLot": 1}

    fils_by_id: dict[int, dict[str, Any]] = {}
    for fil in brassin_fils:
        sid = fil.get("idStockBouteille")
        if sid:
            # Copie pour ne pas muter la réponse EB
            new_fil = dict(fil)
            new_fil["quantiteMiseEnBouteille"] = None
            new_fil["lot"] = DEFAULT_UNUSED_LOT
            new_fil["dateDisponibilite"] = None
            fils_by_id[int(sid)] = new_fil

    for item in items:
        resolution = resolve_bottle_stock(
            tenant_id=tenant_id,
            brassin_fils=brassin_fils,
            id_produit=int(id_produit),
            fmt=str(item.get("fmt") or ""),
            marque=str(item.get("marque") or ""),
        )
        if resolution is None:
            raise ValueError(
                f"execute_mise_en_bouteille: résolution impossible pour "
                f"(produit={id_produit}, fmt={item.get('fmt')}, "
                f"marque={item.get('marque')}). Vérifier que "
                f"eb_stock_product_templates est à jour.",
            )
        target = fils_by_id.get(resolution.id_stock_bouteille)
        if target is None:
            raise ValueError(
                f"execute_mise_en_bouteille: idStockBouteille "
                f"{resolution.id_stock_bouteille} (depuis resolver) absent des "
                "fils EB. Incohérence eb_stock_product_templates vs EB live.",
            )
        target["quantiteMiseEnBouteille"] = int(item["cartons"])
        # lot.idLot vient du template eb_stock_product_templates (ex idLot=3
        # pour Carton de 6, idLot=4 pour Carton de 12).
        if resolution.id_lot is not None:
            target["lot"] = {"idLot": int(resolution.id_lot)}
        # dateDisponibilite = date de mise en bouteille (les bouteilles sont
        # dispo dès qu'elles sont conditionnées).
        target["dateDisponibilite"] = date_mise

    # Reconstruit l'arbre modelesStockProduitBouteille avec nos quantites
    new_root = dict(entrepot_root)
    new_root["modelesFils"] = list(fils_by_id.values())
    base_payload["modelesStockProduitBouteille"] = [new_root]

    # Vide les planificationsProductions résiduelles dans modeleBrassin :
    # quand un brassin a été déconditionné (cas observé en debug), il garde
    # des entrées résiduelles que EB refuse de retraiter. EB UI envoie
    # toujours une liste vide pour ce champ lors d'une nouvelle mise-en-bouteille.
    mb = base_payload.get("modeleBrassin")
    if isinstance(mb, dict):
        mb_copy = dict(mb)
        mb_copy["planificationsProductions"] = []
        base_payload["modeleBrassin"] = mb_copy

    # Inject les valeurs métier de l'event.
    # Pour ``numeroLot`` : EB attend le nom du brassin (ex "KDF18052026"),
    # PAS le DDM ou un autre lot. On override avec ``modeleBrassin.nom`` si
    # disponible, sinon on conserve la valeur de l'event (rétro-compat).
    brassin_nom = (base_payload.get("modeleBrassin") or {}).get("nom")
    base_payload["numeroLot"] = brassin_nom or numero_lot
    base_payload["dateMiseEnBouteille"] = date_mise
    if dluo := payload.get("dateLimiteUtilisationOptimale"):
        base_payload["dateLimiteUtilisationOptimale"] = dluo
    # Toujours forcer ces deux champs (EB UI fait pareil)
    base_payload["modelesStocksMiseEnBouteille"] = []
    base_payload.setdefault("modelesStockProduitFutContenant", [])

    # ── 4. POST deduction-stocks-conditionnement → BOM ───────────────────
    deduction_result = execute_endpoint(
        method="POST",
        path="brassin/deduction-stocks-conditionnement",
        payload=base_payload,
    )
    stocks_destock = deduction_result.get("modelesStocksMiseEnBouteille") or []
    _log.info(
        "execute_mise_en_bouteille: deduction-stocks pour brassin=%s → %d items destock",
        brassin_id, len(stocks_destock),
    )

    # ── 5. POST mise-en-bouteille avec la BOM ────────────────────────────
    final_payload = {**base_payload, "modelesStocksMiseEnBouteille": stocks_destock}

    return execute_endpoint(
        method="POST",
        path="brassin/mise-en-bouteille",
        payload=final_payload,
        # EB confirme par {"message":"","map":{}} ou body vide selon les cas
        allow_empty_2xx=True,
    )
