#!/usr/bin/env python3
"""
scripts/retry_pending_emails.py
================================
Retry les emails en status='pending' dans la table email_queue.

À ordonnancer toutes les 5-15 min via cron ou systemd timer :

    */10 * * * * cd /home/ubuntu/app && /home/ubuntu/app/venv/bin/python \\
        scripts/retry_pending_emails.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
_log = logging.getLogger("ferment.email_retry")


def main() -> int:
    batch = 20
    if len(sys.argv) > 1:
        try:
            batch = int(sys.argv[1])
        except ValueError:
            _log.error("Argument batch_size invalide : %s", sys.argv[1])
            return 2

    from common.email_queue import retry_pending_emails
    try:
        summary = retry_pending_emails(batch_size=batch)
    except Exception:
        _log.exception("Échec retry_pending_emails")
        return 1
    _log.info(
        "Email retry summary: attempted=%d sent=%d retried=%d failed=%d",
        summary["attempted"], summary["sent"],
        summary["retried"], summary["failed"],
    )
    print(
        f"attempted={summary['attempted']} "
        f"sent={summary['sent']} "
        f"retried={summary['retried']} "
        f"failed={summary['failed']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
