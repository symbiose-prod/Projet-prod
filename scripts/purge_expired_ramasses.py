#!/usr/bin/env python3
"""
scripts/purge_expired_ramasses.py
=================================
Hard-delete des ramasses soft-deleted depuis plus de N jours (default 7).

À ordonnancer via cron ou systemd timer (voir ops/ramasse-purge.timer) :

    # crontab (tous les jours à 03:15 UTC)
    15 3 * * * cd /home/ubuntu/app && /usr/bin/python3 scripts/purge_expired_ramasses.py

Argument optionnel :
    python3 scripts/purge_expired_ramasses.py [retention_days]

Sortie :
    Purged N expired ramasse(s) (retention=7d)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Rendre le package racine importable quand le script est lancé directement
# (ex: via systemd avec ExecStart=/…/python /…/scripts/purge_expired_ramasses.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Charger .env (DB_* + tenant scope) avant d'importer les modules DB
from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=False)

# Logging simple vers stdout (lisible par journalctl si lancé par systemd)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
_log = logging.getLogger("ferment.purge")


def main() -> int:
    retention = 7
    if len(sys.argv) > 1:
        try:
            retention = int(sys.argv[1])
        except ValueError:
            _log.error("Argument rétention invalide : %s", sys.argv[1])
            return 2
    if retention < 1:
        _log.error("Rétention minimale : 1 jour (reçu %d)", retention)
        return 2

    from common.ramasse_history import purge_expired_ramasses
    try:
        purged = purge_expired_ramasses(retention_days=retention)
    except Exception:
        _log.exception("Échec purge des ramasses expirées")
        return 1
    _log.info("Purged %d expired ramasse(s) (retention=%dd)", purged, retention)
    print(f"Purged {purged} expired ramasse(s) (retention={retention}d)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
