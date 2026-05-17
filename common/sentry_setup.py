"""
common/sentry_setup.py
======================
Initialisation Sentry (error tracking + perf monitoring).

À appeler **tôt** dans `app_nicegui.py`, juste après ``load_dotenv()`` et
avant la création de l'app NiceGUI, pour catcher aussi les erreurs au boot.

Le DSN est lu depuis l'env var ``SENTRY_DSN`` (stocké dans ``.env`` côté
VPS, pas committé). Si vide → Sentry désactivé silencieusement (dev local
ou CI sans DSN configuré).

Plan Developer (gratuit) : 5 000 erreurs/mois + 10 000 perf events/mois.
Pour rester dans le quota, on sample 10% des transactions de performance —
les erreurs, elles, sont toutes capturées.
"""
from __future__ import annotations

import logging
import os

_log = logging.getLogger("ferment.sentry")


def init_sentry() -> None:
    """Initialise Sentry si ``SENTRY_DSN`` est défini.

    Idempotent : peut être appelé plusieurs fois sans crash (le SDK le
    gère). Mais on log la 1ère init pour aider à diagnostiquer.
    """
    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        _log.info("Sentry désactivé (SENTRY_DSN absent du .env)")
        return

    try:
        import sentry_sdk
    except ImportError:
        _log.warning(
            "Sentry SDK pas installé — `pip install sentry-sdk[fastapi]` "
            "puis restart pour activer.",
        )
        return

    environment = (os.getenv("ENV") or "production").strip()

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        # Sample 10% des transactions de performance pour respecter le
        # quota gratuit (10k/mois sur Developer plan). Les erreurs sont
        # toujours capturées à 100%, c'est juste les traces perf qui sont
        # samplées. À ajuster si on veut plus de visibilité perf.
        traces_sample_rate=0.1,
        # PII (email user, IP) inclus dans les events : OK car l'app est
        # interne (PME) et ça aide énormément pour le debugging
        # ("quel opérateur a eu cette erreur, sur quel endpoint ?").
        send_default_pii=True,
        # Ignore les exceptions de routine qui ne sont pas des bugs réels
        ignore_errors=[
            KeyboardInterrupt,
            # SystemExit déclenché par les shutdown gracieux NiceGUI
            SystemExit,
        ],
    )
    _log.info(
        "Sentry initialisé (env=%s, traces_sample=10%%)", environment,
    )
