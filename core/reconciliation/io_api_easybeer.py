"""
io_api_easybeer.py — ENTRÉE « API » pour les COMMANDES (Easy Beer).

Remplace l'upload manuel de l'export Excel : on demande à l'API EasyBeer le
MÊME export que celui de l'UI (POST /commande/export/{etat} → .xlsx, 47
colonnes identiques), puis on le parse avec io_files.lire_commandes.
→ Sortie STRICTEMENT identique à la source fichier : dict[int, Commande],
  avec `brut` = toutes les colonnes (méga tableau inchangé).

Pourquoi l'export et pas /commande/liste/{etat} ? Vérifié sur données réelles :
la liste renvoie une projection ALLÉGÉE (poids et tournée à null) — inutilisable
pour la réconciliation. L'export, lui, est complet, et tient en UNE requête
(rate-limit EasyBeer : 1 req/s — paginer 3 000 commandes prendrait ~30 s).

Plomberie réutilisée : common/easybeer/_client.py (auth Basic via .env,
rate-limiter global, circuit-breaker, logging). L'import core → common est
permis par les guards d'architecture (tests/test_architecture_layers.py).

Gotchas API découverts par tests réels (2026-06-11) — la spec OpenAPI ne les
documente pas, et l'API répond 500 générique au lieu de 400 :
  - numeroPage commence à 1 (0 → HTTP 500)
  - typeExport='SIMPLE' (une ligne par commande) ; 'EXCEL'/'XLSX'/... → 500
  - filtre dates en ISO 'YYYY-MM-DDTHH:mm:ss.SSSZ' (les réponses JSON, elles,
    datent en epoch millisecondes)
  - {etat} = 'TOUTES' pour ne pas filtrer par état
"""
from __future__ import annotations

import os
import tempfile

from common.easybeer._client import BASE, _auth, _check_response, get_session

from .io_files import lire_commandes


def lire_commandes_easybeer(date_min=None, date_max=None, etat="TOUTES"):
    """Commandes Easy Beer via l'API → dict[int, Commande].

    Même contrat que io_files.lire_commandes (source fichier) : la valeur de
    retour se passe telle quelle à reconciliation_core.reconcilier().

    date_min / date_max : bornes facultatives sur la DATE DE CRÉATION de la
        commande ("YYYY-MM-DD"). ⚠️ Une commande livrée en avril peut avoir été
        créée en mars : pour une réconciliation, prendre large (ou aucun filtre,
        comme l'export UI complet).
    etat : filtre d'état EasyBeer ('TOUTES' par défaut).
    """
    body = {}
    if date_min:
        body["dateDebutCreation"] = f"{date_min}T00:00:00.000Z"
    if date_max:
        body["dateFinCreation"] = f"{date_max}T23:59:59.999Z"

    s = get_session()
    r = s.post(
        f"{BASE}/commande/export/{etat}",
        json=body,
        params={
            "colonneTri": "dateCreation",
            "nombreParPage": 100,
            "numeroPage": 1,        # 1-based — 0 → HTTP 500
            "exporterTout": "true",
            "typeExport": "SIMPLE",  # une ligne par commande (= export UI)
        },
        auth=_auth(),
        timeout=120,
    )
    _check_response(r, f"commande/export/{etat}")

    fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="eb_export_api_")
    os.close(fd)
    try:
        with open(path, "wb") as fh:
            fh.write(r.content)
        return lire_commandes(path)
    finally:
        os.unlink(path)
