#!/usr/bin/env python3
"""
scripts/test_bl_diff_pdf.py
============================
Script de validation visuelle du PDF différentiel.

Génère deux PDFs :
  - /tmp/bl_v1.pdf  (version initiale)
  - /tmp/bl_v2.pdf  (mise à jour avec ajouts + modifications)

Scénario simulé (workflow réel Ferment Station) :
  - J1 soir : 3 produits produits et déclarés à SOFRIPA
  - J2 matin : 1 produit modifié (+cartons), 1 nouveau produit ajouté
  - J2 midi : renvoi du BL v2 au transporteur
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from common.xlsx_fill.bl_pdf import build_bl_enlevements_pdf

COLUMNS = [
    "R\u00e9f\u00e9rence",
    "Produit (go\u00fbt + format)",
    "DDM",
    "Date ramasse souhait\u00e9e",
    "Quantit\u00e9 cartons",
    "Quantit\u00e9 palettes",
    "Poids palettes (kg)",
]


def _make_row(ref, produit, ddm, cartons, palettes, poids):
    return {
        "R\u00e9f\u00e9rence": ref,
        "Produit (go\u00fbt + format)": produit,
        "DDM": ddm,
        "Date ramasse souhait\u00e9e": "12/04/2026",
        "Quantit\u00e9 cartons": cartons,
        "Quantit\u00e9 palettes": palettes,
        "Poids palettes (kg)": poids,
    }


def main():
    # ── J1 soir : 3 produits déclarés ──────────────────────────
    v1_rows = [
        _make_row("KEF-ORG-12x33", "K\u00e9fir Original \u2014 12x33cl",
                  "15/03/2027", 50, 2, 350),
        _make_row("KEF-GIN-12x33", "K\u00e9fir Gingembre \u2014 12x33cl",
                  "15/03/2027", 30, 1, 210),
        _make_row("KEF-FRA-12x33", "K\u00e9fir Fraise \u2014 12x33cl",
                  "15/03/2027", 20, 1, 140),
    ]
    df_v1 = pd.DataFrame(v1_rows, columns=COLUMNS)

    date_creation = dt.date(2026, 4, 11)
    date_ramasse = dt.date(2026, 4, 12)

    pdf_v1 = build_bl_enlevements_pdf(
        date_creation=date_creation,
        date_ramasse=date_ramasse,
        destinataire_title="SOFRIPA",
        destinataire_lines=[
            "Zone d'activit\u00e9",
            "77 Avenue Exemple",
            "77000 Ville",
        ],
        df_lines=df_v1,
        packaging_lines=[
            {"label": "Palette bouteilles vides 33cl", "qty": 5, "unit": "palette"},
        ],
    )
    Path("/tmp/bl_v1.pdf").write_bytes(pdf_v1)
    print(f"[OK] v1 generated: /tmp/bl_v1.pdf ({len(pdf_v1):,} bytes)")

    # ── J2 matin : modifications + ajout ───────────────────────
    # KEF-ORG : 50 → 80 cartons (+30), palettes 2 → 4
    # KEF-GIN : inchangé
    # KEF-FRA : inchangé
    # KEF-POM-6x33 : NOUVEAU produit, 15 cartons
    v2_rows = [
        _make_row("KEF-ORG-12x33", "K\u00e9fir Original \u2014 12x33cl",
                  "15/03/2027", 80, 4, 560),
        _make_row("KEF-GIN-12x33", "K\u00e9fir Gingembre \u2014 12x33cl",
                  "15/03/2027", 30, 1, 210),
        _make_row("KEF-FRA-12x33", "K\u00e9fir Fraise \u2014 12x33cl",
                  "15/03/2027", 20, 1, 140),
        _make_row("KEF-POM-6x33", "K\u00e9fir Pomme \u2014 6x33cl",
                  "20/03/2027", 15, 1, 110),
    ]
    df_v2 = pd.DataFrame(v2_rows, columns=COLUMNS)

    # previous_lines : format { ref, cartons } utilisé par le lookup
    previous_lines = [
        {"ref": "KEF-ORG-12x33", "cartons": 50},
        {"ref": "KEF-GIN-12x33", "cartons": 30},
        {"ref": "KEF-FRA-12x33", "cartons": 20},
    ]

    pdf_v2 = build_bl_enlevements_pdf(
        date_creation=date_creation,
        date_ramasse=date_ramasse,
        destinataire_title="SOFRIPA",
        destinataire_lines=[
            "Zone d'activit\u00e9",
            "77 Avenue Exemple",
            "77000 Ville",
        ],
        df_lines=df_v2,
        packaging_lines=[
            {"label": "Palette bouteilles vides 33cl", "qty": 5, "unit": "palette"},
        ],
        previous_lines=previous_lines,
        version=2,
    )
    Path("/tmp/bl_v2.pdf").write_bytes(pdf_v2)
    print(f"[OK] v2 generated: /tmp/bl_v2.pdf ({len(pdf_v2):,} bytes)")

    print("\n--- Diff summary ---")
    print("KEF-ORG-12x33 : MODIFIED (50 -> 80 cartons) [surligne BLEU]")
    print("KEF-GIN-12x33 : unchanged")
    print("KEF-FRA-12x33 : unchanged")
    print("KEF-POM-6x33  : ADDED (nouveau) [surligne JAUNE]")
    print("\nOuvrir les deux PDFs pour validation visuelle :")
    print("  open /tmp/bl_v1.pdf")
    print("  open /tmp/bl_v2.pdf")


if __name__ == "__main__":
    main()
