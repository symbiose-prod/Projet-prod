#!/usr/bin/env python3
"""
agent/agent.py
==============
Agent de synchronisation étiquettes — Windows.

Interroge le SaaS Ferment Station toutes les N secondes pour récupérer
les opérations de sync en attente, puis les applique sur la base Access (.mdb).

Usage :
    python agent.py              # Mode console (debug)
    python agent.py --once       # Exécution unique (pour tests)
"""
from __future__ import annotations

import argparse
import configparser
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

from mdb_writer import MdbLockedError, replace_all

# ─── Configuration ───────────────────────────────────────────────────────────

_DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.ini"


def load_config(path: str | Path | None = None) -> configparser.ConfigParser:
    """Charge la configuration depuis config.ini."""
    config = configparser.ConfigParser()
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH

    if not config_path.exists():
        print(f"ERREUR: Fichier de configuration introuvable : {config_path}")
        print(f"Copiez config.ini.example vers config.ini et configurez-le.")
        sys.exit(1)

    config.read(config_path, encoding="utf-8")
    return config


# ─── Logging ─────────────────────────────────────────────────────────────────


def setup_logging(config: configparser.ConfigParser) -> logging.Logger:
    """Configure le logging avec rotation de fichier."""
    level = config.get("logging", "level", fallback="INFO")
    log_file = config.get("logging", "file", fallback="logs/sync_agent.log")

    log_dir = Path(log_file).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("sync_agent")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(console)

    # File handler (rotation 5 MB, 3 backups)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"),
    )
    logger.addHandler(file_handler)

    return logger


# ─── Communication avec le SaaS ─────────────────────────────────────────────


class SyncClient:
    """Client HTTP pour les endpoints /api/sync/* du SaaS."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })
        self._log = logging.getLogger("sync_agent.http")

    def fetch_pending(self) -> dict[str, Any] | None:
        """GET /api/sync/pending — Récupère l'opération en attente.

        Retourne le dict de l'opération ou None si rien à faire.
        """
        try:
            r = self.session.get(
                f"{self.base_url}/api/sync/pending",
                timeout=self.timeout,
            )
        except requests.ConnectionError:
            self._log.warning("Connexion impossible vers %s", self.base_url)
            return None
        except requests.Timeout:
            self._log.warning("Timeout sur GET /api/sync/pending")
            return None

        if r.status_code == 204:
            return None
        if r.status_code == 401:
            self._log.error("Clé API invalide (401)")
            return None
        if r.status_code != 200:
            self._log.error("Réponse inattendue : %s %s", r.status_code, r.text[:200])
            return None

        return r.json()

    def ack(self, operation_id: int, status: str, error_msg: str = "") -> bool:
        """POST /api/sync/ack — Confirme le traitement d'une opération."""
        payload: dict[str, Any] = {
            "operation_id": operation_id,
            "status": status,
        }
        if error_msg:
            payload["error_msg"] = error_msg

        try:
            r = self.session.post(
                f"{self.base_url}/api/sync/ack",
                json=payload,
                timeout=self.timeout,
            )
            if r.status_code == 200:
                self._log.info("ACK op #%s : %s", operation_id, status)
                return True
            self._log.error("ACK failed: %s %s", r.status_code, r.text[:200])
            return False
        except (requests.ConnectionError, requests.Timeout):
            self._log.warning("Impossible d'envoyer l'ACK pour op #%s", operation_id)
            return False


# ─── Boucle principale ──────────────────────────────────────────────────────


def run_sync_cycle(
    client: SyncClient,
    mdb_path: str,
    table_name: str,
    max_lock_retries: int = 5,
    lock_retry_delay: int = 30,
) -> bool:
    """Exécute un cycle de sync : fetch → write → ack.

    Retourne True si une opération a été traitée, False sinon.
    """
    log = logging.getLogger("sync_agent")

    # 1. Récupérer l'opération en attente
    log.info("Polling %s …", client.base_url)
    op = client.fetch_pending()
    if not op:
        log.info("Aucune opération en attente — prochain poll dans %ds", 300)
        return False

    op_id = op.get("operation_id")
    products = op.get("products", [])
    log.info("Opération #%s reçue : %d produits", op_id, len(products))

    if not products:
        log.warning("Opération #%s vide, on ACK quand même", op_id)
        client.ack(op_id, "applied")
        return True

    # 2. Écrire dans le .mdb (avec retry en cas de verrouillage)
    for attempt in range(1, max_lock_retries + 1):
        try:
            count = replace_all(mdb_path, table_name, products)
            log.info("Écriture .mdb OK : %d produits", count)
            client.ack(op_id, "applied")
            return True

        except MdbLockedError as e:
            log.warning(
                "Fichier .mdb verrouillé (tentative %d/%d) : %s",
                attempt, max_lock_retries, e,
            )
            if attempt < max_lock_retries:
                time.sleep(lock_retry_delay)
            else:
                log.error("Échec après %d tentatives, report erreur au SaaS", max_lock_retries)
                client.ack(op_id, "error", f"Fichier .mdb verrouillé après {max_lock_retries} tentatives")
                return True  # L'opération a été traitée (en erreur)

        except Exception as e:
            log.exception("Erreur inattendue lors de l'écriture .mdb")
            client.ack(op_id, "error", str(e)[:500])
            return True

    return False


def main():
    parser = argparse.ArgumentParser(description="Agent sync étiquettes Ferment Station")
    parser.add_argument("--config", default=None, help="Chemin vers config.ini")
    parser.add_argument("--once", action="store_true", help="Exécution unique (pas de boucle)")
    args = parser.parse_args()

    # Charger config
    config = load_config(args.config)
    log = setup_logging(config)

    # Paramètres
    base_url = config.get("server", "url")
    api_key = config.get("server", "api_key")
    mdb_path = config.get("local", "mdb_path")
    table_name = config.get("local", "table_name", fallback="Produits")
    poll_interval = config.getint("local", "poll_interval", fallback=300)
    max_lock_retries = config.getint("local", "max_lock_retries", fallback=5)
    lock_retry_delay = config.getint("local", "lock_retry_delay", fallback=30)

    # Vérifier que le .mdb existe
    if not os.path.exists(mdb_path):
        log.error("Fichier .mdb introuvable : %s", mdb_path)
        sys.exit(1)

    log.info("=== Agent sync étiquettes démarré ===")
    log.info("SaaS: %s", base_url)
    log.info("MDB: %s (table: %s)", mdb_path, table_name)
    log.info("Poll: toutes les %ds", poll_interval)

    client = SyncClient(base_url, api_key)

    if args.once:
        run_sync_cycle(client, mdb_path, table_name, max_lock_retries, lock_retry_delay)
        return

    # Boucle infinie
    while True:
        try:
            run_sync_cycle(client, mdb_path, table_name, max_lock_retries, lock_retry_delay)
        except KeyboardInterrupt:
            log.info("Arrêt demandé (Ctrl+C)")
            break
        except Exception:
            log.exception("Erreur non gérée dans le cycle de sync")

        time.sleep(poll_interval)

    log.info("=== Agent sync étiquettes arrêté ===")


if __name__ == "__main__":
    main()
