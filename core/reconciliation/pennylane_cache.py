"""
pennylane_cache.py — cache disque des factures Pennylane parsées.

Une facture émise est immuable → une fois téléchargée + parsée, elle n'est
JAMAIS retéléchargée ni reparsée. On ne conserve pas le PDF, seulement le
résultat du parsing (list[LigneFacture] sérialisée en JSON) : c'est tout ce
dont la réconciliation a besoin, et c'est ce qui coûte cher à produire.

Un fichier JSON par facture : data/reconciliation_cache/factures/{id}.json
(le dossier est gitignoré). Le cache est PARTAGÉ entre tous les utilisateurs
du serveur — choix assumé : les factures SOFRIPA sont les mêmes pour toute
la société (à signaler au mainteneur en PR, l'app étant multi-tenant).

Pas d'expiration. En revanche chaque entrée porte la version du parseur :
si la logique de parsing PDF (io_files.lire_facture) évolue, incrémenter
PARSER_VERSION invalide automatiquement les entrées obsolètes.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from .reconciliation_core import LigneFacture

_log = logging.getLogger("ferment.pennylane_cache")

# ⚠️ À incrémenter à CHAQUE modification du parsing (io_files.lire_facture ou
# stockage.lire_stockage) : les entrées d'une autre version sont re-téléchargées.
# v2 : ajout du champ "stockage" (facture mensuelle STOCKAGE SITE WISSOUS).
PARSER_VERSION = 2

DEFAULT_CACHE_DIR = Path("data/reconciliation_cache/factures")


class PennylaneCache:
    """Cache fichier best-effort : toute erreur disque dégrade en « pas de cache »."""

    def __init__(self, root: Path | str = DEFAULT_CACHE_DIR):
        self.root = Path(root)

    def _path(self, invoice_id) -> Path:
        return self.root / f"{invoice_id}.json"

    def has(self, invoice_id) -> bool:
        """Présence (sans vérifier la version) — sert à l'estimation initiale."""
        return invoice_id is not None and self._path(invoice_id).exists()

    def get(self, invoice_id) -> tuple[list[LigneFacture], dict | None] | None:
        """(lignes transport, info stockage|None), ou None si absente/obsolète."""
        if invoice_id is None:
            return None
        p = self._path(invoice_id)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _log.warning("Entrée de cache illisible, ignorée : %s", p)
            return None
        if data.get("version") != PARSER_VERSION:
            return None
        try:
            lignes = [LigneFacture(**ligne) for ligne in data.get("lignes", [])]
        except TypeError:
            _log.warning("Entrée de cache incompatible, ignorée : %s", p)
            return None
        return lignes, data.get("stockage")

    def put(self, invoice_id, lignes, date=None, stockage=None) -> None:
        if invoice_id is None:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {
                "invoice_id": invoice_id,
                "date": date,
                "version": PARSER_VERSION,
                "lignes": [asdict(ligne) for ligne in lignes],
                "stockage": stockage,
            }
            self._path(invoice_id).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8",
            )
        except OSError:
            _log.exception("Écriture du cache échouée (id=%s)", invoice_id)

    def stats(self) -> tuple[int, int]:
        """(nombre de factures en cache, taille totale en octets)."""
        if not self.root.exists():
            return 0, 0
        files = list(self.root.glob("*.json"))
        return len(files), sum(f.stat().st_size for f in files)

    def clear(self) -> int:
        """Vide le cache. Retourne le nombre d'entrées supprimées."""
        n = 0
        if self.root.exists():
            for f in self.root.glob("*.json"):
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    _log.exception("Suppression échouée : %s", f)
        return n
