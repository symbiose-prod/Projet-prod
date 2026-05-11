"""
print_agent.py — Agent local Windows pour Brother QL-1110NWBc
=============================================================

Tourne en permanence sur la machine Windows allumée dans l'entrepôt.
Long-poll le VPS Ferment Station pour récupérer les jobs d'impression
soumis depuis l'iPhone, puis les imprime via le driver Brother (Windows
ShellExecute → file d'attente Windows → driver Brother → imprimante).

Setup :
  1. Installer le driver Brother QL-1110NWBc depuis brother.fr
  2. Définir la Brother comme imprimante par défaut OU mettre son nom
     exact dans .env (PRINTER_NAME)
  3. Installer Python 3.11+
  4. pip install -r requirements.txt
  5. Copier .env.sample → .env, remplir VPS_URL et AGENT_TOKEN
  6. Tester :  python print_agent.py
  7. Pour démarrage auto : raccourci → Démarrage Windows ou tâche planifiée

Le script est idempotent — on peut le tuer/relancer à volonté, les jobs
'printing' bloqués > 5 min sont reset par le serveur (watchdog).
"""
from __future__ import annotations

import base64
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

# ─── Config ─────────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

VPS_URL = os.environ.get("VPS_URL", "").rstrip("/")
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "").strip()
PRINTER_NAME = os.environ.get("PRINTER_NAME", "").strip()  # vide = imprimante par défaut Windows
POLL_TIMEOUT_S = int(os.environ.get("POLL_TIMEOUT_S", "30"))  # > 25s côté serveur
RETRY_BACKOFF_S = int(os.environ.get("RETRY_BACKOFF_S", "5"))

if not VPS_URL or not AGENT_TOKEN:
    print("ERREUR : VPS_URL et AGENT_TOKEN sont requis dans .env", file=sys.stderr)
    sys.exit(1)


# ─── Logging ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_HERE / "print_agent.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("print_agent")


# ─── Impression Windows ─────────────────────────────────────────────────

def _print_pdf_windows(pdf_path: Path, n_copies: int = 1) -> None:
    """Imprime un PDF via Windows ShellExecute (driver Brother).

    Le PDF contient déjà ``n_copies`` pages (générées côté serveur dans
    build_etiquette_palette_pdf via la recommandation GS1 « 2 faces de
    palette »), donc on l'imprime **une seule fois** — le driver Brother
    se charge d'envoyer toutes les pages. L'argument ``n_copies`` est
    gardé pour le log mais n'est plus utilisé pour boucler.

    "printto" = verbe Windows standard pour imprimer sur une imprimante
    nommée. Fallback sur "print" (imprimante par défaut) si ça échoue.
    """
    try:
        import win32api  # type: ignore
        import win32print  # type: ignore
    except ImportError:
        log.error("pywin32 non installé. Run: pip install pywin32")
        raise

    printer = PRINTER_NAME or win32print.GetDefaultPrinter()
    log.info(
        "Impression de %s sur %r (%d page%s incluses dans le PDF)",
        pdf_path.name, printer, n_copies, "s" if n_copies > 1 else "",
    )

    try:
        win32api.ShellExecute(
            0,
            "printto",
            str(pdf_path),
            f'"{printer}"',
            ".",
            0,  # SW_HIDE
        )
    except Exception:
        log.exception("ShellExecute échoué — fallback sur 'print' (par défaut)")
        win32api.ShellExecute(0, "print", str(pdf_path), None, ".", 0)


# ─── HTTP client ────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {AGENT_TOKEN}"}


def _fetch_next_job() -> dict | None:
    """Long-poll le VPS pour le prochain job. Retourne None si timeout/204."""
    url = f"{VPS_URL}/api/print-jobs/next"
    try:
        resp = requests.get(url, headers=_headers(), timeout=POLL_TIMEOUT_S)
    except requests.exceptions.Timeout:
        return None
    except requests.exceptions.RequestException as exc:
        log.warning("Connexion VPS impossible : %s", exc)
        return None

    if resp.status_code == 204:
        return None
    if resp.status_code == 401:
        log.error("Token invalide — vérifie AGENT_TOKEN dans .env")
        time.sleep(60)  # éviter le spam
        return None
    if resp.status_code == 503:
        log.error("Le VPS n'a pas configuré PRINT_AGENT_TOKEN — contacte l'admin")
        time.sleep(60)
        return None
    if resp.status_code != 200:
        log.warning("VPS retourne %d : %s", resp.status_code, resp.text[:200])
        return None

    try:
        return resp.json()
    except Exception:
        log.exception("Réponse VPS non-JSON")
        return None


def _ack_done(job_id: int) -> None:
    """Confirme l'impression au VPS."""
    try:
        requests.post(
            f"{VPS_URL}/api/print-jobs/{job_id}/done",
            headers=_headers(),
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        log.warning("Ack done %d échoué : %s (le job sera reset par le watchdog)", job_id, exc)


def _ack_error(job_id: int, error: str) -> None:
    """Signale une erreur d'impression au VPS."""
    try:
        requests.post(
            f"{VPS_URL}/api/print-jobs/{job_id}/error",
            headers=_headers(),
            json={"error": error[:500]},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        log.warning("Ack error %d échoué : %s", job_id, exc)


# ─── Boucle principale ──────────────────────────────────────────────────

def _process_job(job: dict) -> None:
    """Décode le PDF, l'imprime, ack le serveur."""
    job_id = int(job["id"])
    filename = str(job.get("filename") or f"etiquette_{job_id}.pdf")
    n_copies = int(job.get("n_copies") or 1)

    try:
        pdf_bytes = base64.b64decode(job.get("pdf_b64") or "")
    except Exception as exc:
        _ack_error(job_id, f"Décodage base64 impossible : {exc}")
        return

    if not pdf_bytes:
        _ack_error(job_id, "PDF vide")
        return

    # Tempfile : nettoyé après impression. Délai court avant suppression
    # pour laisser le visualiseur PDF lire le fichier en arrière-plan.
    with tempfile.NamedTemporaryFile(
        suffix=".pdf", prefix=f"job{job_id}_", delete=False, dir=_HERE / "tmp",
    ) as f:
        f.write(pdf_bytes)
        tmp_path = Path(f.name)

    try:
        _print_pdf_windows(tmp_path, n_copies=n_copies)
        _ack_done(job_id)
        log.info("✓ Job %d imprimé", job_id)
    except Exception as exc:
        log.exception("Erreur impression job %d", job_id)
        _ack_error(job_id, str(exc))
    finally:
        # Délai pour que le spool Windows lise le fichier avant qu'on le supprime
        time.sleep(2)
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> None:
    log.info("Print agent démarré — VPS=%s, printer=%r", VPS_URL, PRINTER_NAME or "(par défaut Windows)")
    (_HERE / "tmp").mkdir(exist_ok=True)
    while True:
        try:
            job = _fetch_next_job()
            if job:
                _process_job(job)
            # Si pas de job (204 ou timeout), on reboucle immédiatement —
            # le serveur tient le long-poll, donc pas de risque de spam.
        except KeyboardInterrupt:
            log.info("Interruption clavier — arrêt propre")
            break
        except Exception:
            log.exception("Erreur dans la boucle principale — backoff")
            time.sleep(RETRY_BACKOFF_S)


if __name__ == "__main__":
    main()
