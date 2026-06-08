#!/usr/bin/env python3
"""
scripts/diagnose_etiquettes_6x33.py
===================================
Diagnostic READ-ONLY : pourquoi un format (ex: 6x33) n'apparaît pas dans la
sync étiquettes (page /sync).

À lancer SUR LE VPS (là où DB_HOST pointe sur la base de prod et où les
identifiants EasyBeer sont dans l'environnement) :

    cd /home/ubuntu/app        # ou le chemin réel du repo sur le VPS
    python3 scripts/diagnose_etiquettes_6x33.py
    python3 scripts/diagnose_etiquettes_6x33.py 6x33      # ne cibler qu'un format
    python3 scripts/diagnose_etiquettes_6x33.py 6x33 42494  # format + un produit

Ce script ne fait QUE des lectures :
  - SELECT sur eb_cache (matrice codes-barres cachée 24h) et sync_operations
  - lecture du fichier data/_stock_codes_cache.json (codes articles)
Il n'écrit rien, ne déclenche aucune sync, ne tape PAS l'API EasyBeer.

Pour chaque produit concerné il indique, pour le format ciblé :
  [MATRICE]  un code-barres COLIS (quantité>1) existe-t-il ?  -> crée le format
  [CODE ART] un codeArticle existe-t-il pour (produit, format) ? -> obligatoire
  [PAYLOAD]  le format est-il sorti dans la dernière sync ?
Le verdict croise ces 3 colonnes pour pointer la cause exacte.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=False)

from common.sync.collector import (  # noqa: E402
    _STOCK_CODES_CACHE_PATH,
    _parse_barcode_matrix_for_labels,
)
from db.conn import run_sql  # noqa: E402

# ─── Arguments ───────────────────────────────────────────────────────────────
TARGET_FMT = sys.argv[1] if len(sys.argv) > 1 else "6x33"
TARGET_PID = int(sys.argv[2]) if len(sys.argv) > 2 else None


def _matrix_rows() -> list[dict]:
    """Toutes les entrées eb_cache de la matrice codes-barres (tous tenants)."""
    rows = run_sql(
        "SELECT tenant_id, data FROM eb_cache WHERE cache_key = 'code_barre_matrice'"
    )
    return rows if isinstance(rows, list) else []


def _stock_codes() -> dict[tuple[int, str], str]:
    """Lit le cache fichier des codes articles {(pid, fmt): codeArticle}."""
    try:
        with open(_STOCK_CODES_CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return {}
    return {
        (e["pid"], e["fmt"]): e["code"]
        for e in cache.get("data", [])
    }


def _last_payload_formats() -> set[tuple[int, str]]:
    """Formats (pid, fmt) sortis dans la dernière opération de sync.

    Le payload ne stocke pas l'idProduit, mais la désignation se termine par
    le format ('… — 6x33cl'). On retrouve donc le fmt ; le pid est croisé via
    la matrice plus bas, on renvoie ici l'ensemble des fmt vus + designations.
    """
    rows = run_sql(
        """SELECT payload FROM sync_operations
           WHERE status IN ('applied', 'pending')
           ORDER BY created_at DESC LIMIT 1"""
    )
    out: set[str] = set()
    if not rows:
        return set()
    payload = rows[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    for p in payload or []:
        desig = p.get("designation", "")
        m = re.search(r"(\d+x\d+)cl\s*$", desig)
        if m:
            out.add((desig, m.group(1)))
    return out  # type: ignore[return-value]


def main() -> int:
    print(f"=== Diagnostic étiquettes — format ciblé : {TARGET_FMT} ===\n")

    rows = _matrix_rows()
    if not rows:
        print("❌ Aucune matrice codes-barres en cache (eb_cache).")
        print("   → ouvre la page /sync et relance une sync pour peupler le cache.")
        return 1

    stock_codes = _stock_codes()
    if not stock_codes:
        print("⚠️  Cache codes articles (data/_stock_codes_cache.json) absent ou vide.")
        print("    La colonne [CODE ART] sera 'inconnu' (relance une sync pour le peupler).\n")

    payload_designations = _last_payload_formats()
    payload_fmts = {fmt for (_d, fmt) in payload_designations}

    for cache_row in rows:
        tenant = cache_row["tenant_id"]
        data = cache_row["data"]
        if isinstance(data, str):
            data = json.loads(data)

        by_product = _parse_barcode_matrix_for_labels(data)

        # Noms produits depuis la matrice brute (pour l'affichage)
        names: dict[int, str] = {}
        for prod_entry in data.get("produits", []):
            for cb in prod_entry.get("codesBarres", []):
                mp = cb.get("modeleProduit") or {}
                idp = mp.get("idProduit")
                if idp and idp not in names:
                    names[idp] = mp.get("libelle") or mp.get("nom") or str(idp)

        print(f"── Tenant {tenant} — {len(by_product)} produits dans la matrice ──")
        target_vol = TARGET_FMT.split("x")[-1]  # '6x33' -> '33'

        any_target = False
        for pid, formats in sorted(by_product.items()):
            if TARGET_PID and pid != TARGET_PID:
                continue
            # On ne montre que les produits déclinés dans le volume ciblé
            same_vol = [f for f in formats if f.endswith("x" + target_vol)]
            if not same_vol and not TARGET_PID:
                continue
            any_target = True

            has_matrix = TARGET_FMT in formats
            code_art = stock_codes.get((pid, TARGET_FMT))
            in_payload = TARGET_FMT in payload_fmts  # (global, indicatif)

            label = names.get(pid, f"#{pid}")
            print(f"\n  • {pid}  {label}")
            print(f"      formats colis dans la matrice : {sorted(formats) or '∅'}")
            print(f"      [MATRICE ] {TARGET_FMT} colis présent ? "
                  f"{'OUI' if has_matrix else 'NON  ← le format n’est jamais créé'}")
            if stock_codes:
                print(f"      [CODE ART] codeArticle ({pid},{TARGET_FMT}) ? "
                      f"{repr(code_art) if code_art else 'ABSENT  ← ligne sautée (NOT NULL)'}")
            else:
                print("      [CODE ART] inconnu (cache codes articles absent)")

            # Verdict par produit
            if has_matrix and code_art:
                print("      ✅ Tout est là — devrait sortir. Si absent : voir dédoublonnage "
                      "(même codeArticle qu’un autre format ?) ou brassin pas 'en cours'.")
            elif not has_matrix:
                print("      🔴 CAUSE : pas de code-barres COLIS quantité=6 dans EasyBeer "
                      "(Paramètres → Codes-barres).")
            elif not code_art:
                print("      🔴 CAUSE : pas de code article sur la ligne de stock "
                      f"{TARGET_FMT} dans EasyBeer.")

        if not any_target:
            print(f"  (aucun produit avec un code-barres colis se terminant par x{target_vol})")

    print("\n=== Rappel payload dernière sync ===")
    if payload_designations:
        fmts_sorted = sorted({fmt for (_d, fmt) in payload_designations})
        print(f"  Formats sortis : {fmts_sorted}")
        print(f"  '{TARGET_FMT}' présent dans la dernière sync ? "
              f"{'OUI' if TARGET_FMT in payload_fmts else 'NON'}")
    else:
        print("  Aucune opération de sync 'applied'/'pending' trouvée.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
