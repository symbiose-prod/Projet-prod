"""
common/lot_fifo.py
==================
Distribution FIFO des numéros de lot pour les ingrédients de brassins EasyBeer.

Assigne les lots disponibles (DLUO la plus ancienne d'abord) aux ingrédients
d'une recette, en splitant les lignes quand un lot ne couvre pas toute la
quantité nécessaire.  Gère la consommation virtuelle quand plusieurs brassins
sont créés d'affilée (EasyBeer ne décrémente le stock qu'à la validation).
"""
from __future__ import annotations

import logging
from typing import Any, Callable

_log = logging.getLogger("ferment.lot_fifo")


# ---------------------------------------------------------------------------
#  LotPool — lots d'une seule matière première
# ---------------------------------------------------------------------------

class LotPool:
    """Stock de lots pour une matière première, trié FIFO (DLUO ascendante)."""

    def __init__(self, id_mp: int, lots_from_api: list[dict[str, Any]]):
        self.id_mp = id_mp
        # Trier par DLUO ascendante (plus vieux d'abord). Sans DLUO → en dernier.
        self._lots = sorted(
            [lot for lot in lots_from_api if (lot.get("quantite") or 0) > 0],
            key=lambda l: l.get("dateLimiteUtilisationOptimale") or float("inf"),
        )
        # Quantité restante virtuelle par lot
        self._remaining: dict[int, float] = {
            lot["idMatierePremiereNumeroLot"]: float(lot.get("quantite", 0))
            for lot in self._lots
        }

    @property
    def has_lots(self) -> bool:
        return len(self._lots) > 0

    def allocate(self, needed_qty: float) -> list[dict[str, Any]]:
        """Distribue *needed_qty* sur les lots FIFO.

        Retourne une liste de dicts ``ModeleNumeroLot`` (un par lot utilisé).
        Met à jour les quantités restantes virtuelles internes.
        """
        if needed_qty <= 0:
            return []

        allocations: list[dict[str, Any]] = []
        still_needed = needed_qty

        for lot in self._lots:
            if still_needed <= 1e-6:
                break

            lot_id = lot["idMatierePremiereNumeroLot"]
            avail = self._remaining.get(lot_id, 0)
            if avail <= 0:
                continue

            take = min(avail, still_needed)
            self._remaining[lot_id] -= take
            still_needed -= take

            allocations.append({
                "code": lot.get("numeroLot", ""),
                "quantite": round(take, 2),
                "idMatierePremiere": self.id_mp,
                "dateLimiteUtilisationOptimale": lot.get(
                    "dateLimiteUtilisationOptimale"
                ),
            })

        if still_needed > 0.01:
            _log.warning(
                "MP %d : stock insuffisant — il manque %.2f (demandé=%.2f)",
                self.id_mp, still_needed, needed_qty,
            )

        return allocations


# ---------------------------------------------------------------------------
#  BatchLotTracker — tracker pour un batch entier de brassins
# ---------------------------------------------------------------------------

class BatchLotTracker:
    """Cache les lots par matière première et gère la consommation virtuelle
    entre plusieurs brassins créés dans le même batch.

    Usage::

        tracker = BatchLotTracker(fetch_lots_fn=easybeer.get_mp_lots)

        for flavor in flavors:
            for ing in recipe_ingredients:
                lines = tracker.distribute_ingredient(ing)
                all_ingredients.extend(lines)
    """

    def __init__(self, fetch_lots_fn: Callable[[int], list[dict[str, Any]]]):
        self._fetch = fetch_lots_fn
        self._pools: dict[int, LotPool] = {}

    def _get_pool(self, id_mp: int) -> LotPool:
        if id_mp not in self._pools:
            try:
                lots = self._fetch(id_mp)
            except Exception:
                _log.warning("Échec fetch lots MP %d", id_mp, exc_info=True)
                lots = []
            self._pools[id_mp] = LotPool(id_mp, lots)
        return self._pools[id_mp]

    def distribute_ingredient(
        self,
        ingredient: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Prend un ingrédient (dict tel que construit par do_create_brassins)
        et retourne 1 ou N lignes avec lots assignés.

        - Si la MP n'a pas de lots → retourne l'ingrédient inchangé.
        - Si split → retourne plusieurs dicts (même champs sauf quantite + lots).
        - Chaque ligne a exactement 1 entrée dans ``modeleNumerosLots``.
        """
        mp = ingredient.get("matierePremiere") or {}
        id_mp = mp.get("idMatierePremiere")
        needed = ingredient.get("quantite", 0)

        if not id_mp or needed <= 0:
            return [ingredient]

        pool = self._get_pool(id_mp)

        if not pool.has_lots:
            return [ingredient]

        allocations = pool.allocate(needed)

        if not allocations:
            _log.warning(
                "MP %d (%s) : aucun lot disponible, modeleNumerosLots reste vide",
                id_mp, mp.get("libelle", "?"),
            )
            return [ingredient]

        # Construire une ligne d'ingrédient par lot
        result: list[dict[str, Any]] = []
        allocated_total = 0.0
        for alloc in allocations:
            ing_copy = {
                k: v for k, v in ingredient.items()
                if k not in ("quantite", "modeleNumerosLots")
            }
            ing_copy["quantite"] = alloc["quantite"]
            ing_copy["modeleNumerosLots"] = [alloc]
            result.append(ing_copy)
            allocated_total += alloc["quantite"]

        # Quantité manquante (pas de lot dispo) — on NE crée PAS de ligne
        # supplémentaire car EasyBeer rejette les ingrédients sans lot quand
        # la MP est gérée par lots. Le manquant est loggé pour traçabilité.
        shortfall = round(needed - allocated_total, 2)

        libelle = mp.get("libelle", f"MP#{id_mp}")
        etape = (ingredient.get("brassageEtape") or {}).get("nom", "?")
        lots_desc = " + ".join(
            f"{a['quantite']:.2f} [{a['code']}]" for a in allocations
        )
        if shortfall > 0.01:
            lots_desc += f" + {shortfall:.2f} [MANQUANT]"
            _log.warning("FIFO %s (%s) : %s", libelle, etape, lots_desc)
        else:
            _log.info("FIFO %s (%s) : %s", libelle, etape, lots_desc)

        return result
