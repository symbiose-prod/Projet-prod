"""
common/ramasse_export.py
========================
Export CSV de l'historique des ramasses pour un mois donné.

Logique pure (sans UI) — testable unitaire, et utilisable à la fois depuis
la page Ramasse (bouton download) et un futur job batch/cron.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any

CSV_COLUMNS = [
    ("date_ramasse", "Date"),
    ("destinataire", "Destinataire"),
    ("version", "Version"),
    ("line_count", "Lignes"),
    ("total_cartons", "Cartons"),
    ("total_palettes", "Palettes"),
    ("total_poids_kg", "Poids (kg)"),
    ("driver_passed", "Chauffeur passé"),
    ("deleted", "Supprimée"),
    ("created_at", "Créée le"),
]


def build_csv_bytes(items: list[dict[str, Any]]) -> bytes:
    """Sérialise la liste de ramasses en CSV UTF-8 avec BOM (compat Excel).

    Le BOM permet à Excel d'ouvrir correctement les caractères accentués
    sans passer par "Importer un fichier externe".
    """
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([label for _, label in CSV_COLUMNS])

    for item in items:
        dr = item.get("date_ramasse")
        date_str = dr.strftime("%Y-%m-%d") if hasattr(dr, "strftime") else str(dr or "")
        created = item.get("created_at")
        created_str = (
            created.strftime("%Y-%m-%d %H:%M")
            if hasattr(created, "strftime")
            else str(created or "")
        )
        writer.writerow([
            date_str,
            item.get("destinataire", ""),
            int(item.get("version") or 1),
            int(item.get("line_count") or 0),
            int(item.get("total_cartons") or 0),
            int(item.get("total_palettes") or 0),
            int(item.get("total_poids_kg") or 0),
            "Oui" if item.get("driver_passed") else "Non",
            "Oui" if item.get("deleted_at") else "Non",
            created_str,
        ])

    csv_str = buf.getvalue()
    return "\ufeff".encode() + csv_str.encode("utf-8")


def filename_for_month(year: int, month: int) -> str:
    """Nom de fichier suggéré : ramasses_2026-04.csv."""
    return f"ramasses_{year:04d}-{month:02d}.csv"


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """Retourne (premier_jour, dernier_jour) d'un mois donné."""
    from calendar import monthrange
    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])
    return first, last
