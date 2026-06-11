"""
io_api.py — ENTRÉE "API" (étape Pennylane).

Récupère la FACTURE transporteur via l'API Pennylane, télécharge son PDF,
puis le parse avec le lecteur existant (io_files.lire_facture).
Les COMMANDES, elles, viennent toujours de l'export Easy Beer (fichier),
comme demandé pour cette étape. L'API Easy Beer viendra plus tard.

Points « à confirmer » du 1er jet, VÉRIFIÉS contre la vraie API Pennylane v2
(juin 2026) — voir explore_api.py pour la démarche :
  - Auth = "Authorization: Bearer <token>"                      ✅ confirmé
  - Base URL = https://app.pennylane.com/api/external/v2          ✅ confirmé
  - Pagination = réponse {items, has_more, next_cursor}           ✅ confirmé
  - URL du PDF = champ "public_file_url"                          ✅ confirmé
  - Filtres = paramètre `filter` JSON [{field, operator, value}]  ✅ corrigé
              (et NON des params plats supplier_id= / date_min=)
  - Transporteur SOFRIPA = fournisseur id 19386322 (202 factures) ✅ confirmé
              ("ANTOINE" matchait par erreur la personne "Antoine Jacquemot")
"""
import json
import os
import tempfile

import requests
from dotenv import load_dotenv

from .io_files import lire_commandes, lire_facture  # on réutilise ton parsing PDF + lecture export

# Charge .env -> variables d'environnement (PENNYLANE_API_KEY). À faire AVANT _cle().
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PENNYLANE_BASE_URL = "https://app.pennylane.com/api/external/v2"

# Id du transporteur SOFRIPA dans Pennylane (vérifié : 202 factures SOFRIPA_*.pdf).
SOFRIPA_SUPPLIER_ID = 19386322

# Fallback de recherche par nom si on ne passe pas d'id (on NE met PLUS "ANTOINE" :
# il matchait la personne "Antoine Jacquemot" — pas le transporteur).
FOURNISSEUR_MOTS = ("SOFRIPA",)


def _cle() -> str:
    """Lit la clé depuis l'environnement (.env). JAMAIS écrite dans le code."""
    cle = os.environ.get("PENNYLANE_API_KEY")
    if not cle:
        raise RuntimeError("Clé absente : définis PENNYLANE_API_KEY dans ton .env")
    return cle


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        # Confirmé : l'API v2 de Pennylane s'authentifie par un token Bearer.
        "Authorization": f"Bearer {_cle()}",
        "Accept": "application/json",
    })
    return s


def _get_pages(s, chemin, params=None):
    """GET paginé (pagination par curseur de l'API v2). Renvoie la liste agrégée."""
    params = dict(params or {})
    params.setdefault("limit", 100)  # max 100 par page côté Pennylane
    items, cursor = [], None
    while True:
        if cursor:
            params["cursor"] = cursor
        r = s.get(f"{PENNYLANE_BASE_URL}/{chemin}", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        # Confirmé : réponse paginée = {items: [...], has_more: bool, next_cursor: str|null}
        items.extend(data.get("items") or [])
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            break
    return items


def _build_filter(supplier_id=None, date_min=None, date_max=None) -> str | None:
    """
    Construit le paramètre `filter` de l'API v2 : un tableau JSON de conditions
    [{"field": ..., "operator": ..., "value": ...}].
    Champs/opérateurs confirmés : supplier_id (eq), date (gteq / lteq).
    Dates au format "YYYY-MM-DD".
    """
    conditions = []
    if supplier_id is not None:
        conditions.append({"field": "supplier_id", "operator": "eq", "value": supplier_id})
    if date_min:
        conditions.append({"field": "date", "operator": "gteq", "value": date_min})
    if date_max:
        conditions.append({"field": "date", "operator": "lteq", "value": date_max})
    return json.dumps(conditions) if conditions else None


def trouver_supplier_id(s, mots=FOURNISSEUR_MOTS):
    """Fallback : cherche l'id du transporteur par son nom (si aucun id fourni)."""
    for f in _get_pages(s, "suppliers"):
        nom = (f.get("name") or "").upper()
        if any(m in nom for m in mots):
            return f.get("id")
    return None


def lire_factures_pennylane(date_min=None, date_max=None, supplier_id=SOFRIPA_SUPPLIER_ID,
                            cache=None, progress_cb=None, stockage_out=None):
    """
    Récupère les factures du transporteur, télécharge chaque PDF, et le parse.
    Renvoie une list[LigneFacture] — exactement le même format que io_files.lire_facture.

    date_min / date_max : bornes facultatives sur la date de facture ("YYYY-MM-DD").
    supplier_id         : par défaut l'id SOFRIPA ; None => recherche par nom.
    cache               : PennylaneCache optionnel — une facture déjà parsée est lue
                          localement, jamais retéléchargée (factures immuables).
    progress_cb         : callback optionnel progress_cb(fait, total, depuis_cache).
                          Premier appel (0, total, nb_déjà_en_cache) = estimation ;
                          puis un appel par facture traitée. Exécuté dans le thread
                          de travail : ne PAS toucher l'UI dedans (écrire dans un
                          dict partagé relu par un ui.timer).
    stockage_out        : liste optionnelle — y APPEND les factures de STOCKAGE
                          mensuelles détectées dans le même flux (dicts
                          {periode, ht, tva, ttc, date}). Voir stockage.py.
    """
    s = _session()
    if supplier_id is None:
        supplier_id = trouver_supplier_id(s)

    params = {}
    filtre = _build_filter(supplier_id=supplier_id, date_min=date_min, date_max=date_max)
    if filtre:
        params["filter"] = filtre

    factures_meta = _get_pages(s, "supplier_invoices", params)
    total = len(factures_meta)
    if progress_cb:
        deja = sum(1 for fac in factures_meta if cache is not None and cache.has(fac.get("id")))
        progress_cb(0, total, deja)

    from .stockage import lire_stockage

    lignes = []
    hits = 0
    for i, fac in enumerate(factures_meta, start=1):
        fid = fac.get("id")
        if cache is not None:
            en_cache = cache.get(fid)
            if en_cache is not None:
                cached_lignes, cached_stockage = en_cache
                lignes.extend(cached_lignes)
                if stockage_out is not None and cached_stockage:
                    stockage_out.append(cached_stockage)
                hits += 1
                if progress_cb:
                    progress_cb(i, total, hits)
                continue
        url_pdf = fac.get("public_file_url")   # lien public Pennylane, on télécharge tout de suite
        if not url_pdf:
            if progress_cb:
                progress_cb(i, total, hits)
            continue
        pdf = s.get(url_pdf, timeout=60)
        pdf.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf.content)
            chemin = f.name
        try:
            parsees = lire_facture(chemin)   # <-- ton parsing existant, réutilisé tel quel
            stk = lire_stockage(chemin)      # facture mensuelle de stockage ? (sinon None)
        finally:
            os.unlink(chemin)
        if stk:
            stk["date"] = fac.get("date")
        if cache is not None:
            cache.put(fid, parsees, date=fac.get("date"), stockage=stk)
        lignes.extend(parsees)
        if stockage_out is not None and stk:
            stockage_out.append(stk)
        if progress_cb:
            progress_cb(i, total, hits)
    return lignes


def charger_sources(export_easybeer_path, **kwargs):
    """
    Assemble les deux sources de l'étape actuelle :
      - factures  -> API Pennylane
      - commandes -> export Easy Beer (fichier)
    Le résultat se passe directement à reconciliation_core.reconcilier(factures, commandes).
    """
    factures = lire_factures_pennylane(**kwargs)
    commandes = lire_commandes(export_easybeer_path)
    return factures, commandes
