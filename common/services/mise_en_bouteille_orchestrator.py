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

from common.easybeer.brassins import get_brassin_detail
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

    # ── 1. Fetch brassin complet (lazy load) ─────────────────────────────
    brassin_full = get_brassin_detail(int(brassin_id))
    if not brassin_full:
        raise ValueError(
            f"execute_mise_en_bouteille: brassin idBrassin={brassin_id} introuvable",
        )

    # Récupère les fils du brassin (stocks bouteille dispo pour ce produit)
    msb_list = brassin_full.get("modelesStockProduitBouteille") or []
    if not msb_list:
        raise ValueError(
            f"execute_mise_en_bouteille: brassin {brassin_id} sans "
            "modelesStockProduitBouteille",
        )
    # Premier élément = entrepôt principal (ex "FERMENT STATION")
    entrepot_root = msb_list[0]
    brassin_fils = entrepot_root.get("modelesFils") or []
    if not brassin_fils:
        raise ValueError(
            f"execute_mise_en_bouteille: brassin {brassin_id} sans fils "
            "(stocks bouteille)",
        )

    id_produit = (brassin_full.get("produit") or {}).get("idProduit")
    if not id_produit:
        raise ValueError(
            f"execute_mise_en_bouteille: brassin {brassin_id} sans produit.idProduit",
        )

    # ── 2-3. Résoudre chaque item & construire les fils ──────────────────
    sent_fils: list[dict[str, Any]] = []
    used_id_stocks: set[int] = set()
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
        sent_fils.append({
            "idStockBouteille": resolution.id_stock_bouteille,
            "libelle": resolution.contenant_libelle,
            "contenance": resolution.contenance,
            "quantiteMiseEnBouteille": int(item["cartons"]),
        })
        used_id_stocks.add(resolution.id_stock_bouteille)

    # Ajoute les fils non utilisés (quantité None) — EB UI les envoie tous,
    # on s'aligne pour ne pas surprendre.
    for fil in brassin_fils:
        sid = fil.get("idStockBouteille")
        if sid and sid not in used_id_stocks:
            sent_fils.append({
                "idStockBouteille": sid,
                "libelle": fil.get("libelle") or "",
                "contenance": float(fil.get("contenance") or 0),
                "quantiteMiseEnBouteille": None,  # pas utilisé dans ce conditionnement
            })

    modeles_stock_produit_bouteille = [{
        "libelle": entrepot_root.get("libelle") or "FERMENT STATION",
        "modelesFils": sent_fils,
    }]

    # ── 4. POST deduction-stocks-conditionnement → BOM ───────────────────
    base_payload: dict[str, Any] = {
        "modeleBrassin": brassin_full,
        "modeleElevage": brassin_full.get("modeleElevage") or {},
        "produitsDerives": (
            [brassin_full["produit"]] if brassin_full.get("produit") else []
        ),
        "volumeRestant": brassin_full.get("volumeRestant") or brassin_full.get("volume") or 0,
        "numeroLot": numero_lot,
        "dateMiseEnBouteille": date_mise,
        "modelesStockProduitBouteille": modeles_stock_produit_bouteille,
        "modelesStockProduitFutContenant": [],
        "modelesStocksMiseEnBouteille": [],  # EB va le calculer
    }
    if dluo := payload.get("dateLimiteUtilisationOptimale"):
        base_payload["dateLimiteUtilisationOptimale"] = dluo

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

    # ── 5-6. POST mise-en-bouteille avec la BOM ──────────────────────────
    final_payload = {**base_payload, "modelesStocksMiseEnBouteille": stocks_destock}

    return execute_endpoint(
        method="POST",
        path="brassin/mise-en-bouteille",
        payload=final_payload,
        # EB confirme par {"message":"","map":{}} ou body vide selon les cas
        allow_empty_2xx=True,
    )
