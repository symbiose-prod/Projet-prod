"""
agent/mdb_writer.py
===================
Écriture dans la base Microsoft Access (.mdb) via pyodbc + ODBC.

Stratégie REPLACE_ALL :
  1. DELETE FROM [Produits]
  2. INSERT INTO [Produits] pour chaque produit
  3. COMMIT atomique (rollback si erreur → table jamais vide)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pyodbc

_log = logging.getLogger("sync_agent.mdb")


class MdbLockedError(Exception):
    """Le fichier .mdb est verrouillé par un autre processus."""


def _is_lock_error(error: pyodbc.Error) -> bool:
    """Détecte si l'erreur est due au verrouillage du fichier."""
    msg = str(error).lower()
    return any(kw in msg for kw in ("locked", "use by another", "verrou", "en cours d'utilisation"))


def replace_all(mdb_path: str, table_name: str, products: list[dict[str, Any]]) -> int:
    """Remplace tout le contenu de la table par les nouveaux produits.

    Retourne le nombre de lignes insérées.
    Lève MdbLockedError si le fichier est verrouillé.
    """
    conn_str = (
        r"DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};"
        f"DBQ={mdb_path};"
    )

    try:
        conn = pyodbc.connect(conn_str, autocommit=False)
    except pyodbc.Error as e:
        if _is_lock_error(e):
            raise MdbLockedError(f"Impossible d'ouvrir {mdb_path}: {e}") from e
        raise

    cursor = conn.cursor()

    try:
        # 1. Supprimer toutes les lignes
        cursor.execute(f"DELETE FROM [{table_name}]")
        _log.debug("DELETE FROM [%s] OK", table_name)

        # 2. Insérer les nouveaux produits
        insert_sql = f"""
        INSERT INTO [{table_name}]
        ([Désignation], [MARQUE], [CODE INTERNE], [PCB],
         [GTIN UVC], [GTIN Colis], [Lot], [DDM])
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """

        inserted = 0
        skipped = 0
        for p in products:
            # CODE INTERNE obligatoire (NOT NULL dans Access)
            code_interne = str(p.get("code_interne", "")).strip()
            if not code_interne:
                _log.warning("Produit sans CODE INTERNE, skip: %s", p.get("designation", "?"))
                skipped += 1
                continue

            # Parser la DDM (ISO string → datetime)
            ddm_raw = p.get("ddm", "")
            try:
                ddm_val = datetime.fromisoformat(ddm_raw) if ddm_raw else None
            except (ValueError, TypeError):
                ddm_val = None

            cursor.execute(insert_sql, (
                str(p.get("designation", ""))[:255],
                str(p.get("marque", ""))[:255],
                code_interne[:255],
                float(p.get("pcb", 0)),
                str(p.get("gtin_uvc", ""))[:255],
                str(p.get("gtin_colis", ""))[:255],
                float(p.get("lot", 0)),
                ddm_val,
            ))
            inserted += 1

        if skipped:
            _log.warning("%d produit(s) sans CODE INTERNE ignoré(s)", skipped)

        # 3. Commit atomique
        conn.commit()
        _log.info("REPLACE_ALL OK : %d produits insérés dans [%s]", inserted, table_name)
        return inserted

    except pyodbc.Error as e:
        conn.rollback()
        _log.error("Erreur écriture .mdb, rollback effectué: %s", e)
        if _is_lock_error(e):
            raise MdbLockedError(str(e)) from e
        raise

    finally:
        cursor.close()
        conn.close()
