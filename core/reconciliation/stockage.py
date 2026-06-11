"""
stockage.py — Coûts de STOCKAGE SOFRIPA (factures mensuelles).

SOFRIPA envoie deux familles de factures (constat sur données réelles 2026-06) :
  - « 003 » (bimensuelles)    → transport, ligne par livraison (io_files.lire_facture)
  - « SO0xxxxx » (mensuelles) → stockage : UNE ligne de prestation
        « STOCKAGE SITE WISSOUS — PERIODE DU 1ER AU 30 AVRIL 2026 » ~10 k€ HT/mois

Ce module détecte ces factures de stockage dans le même flux Pennylane que le
transport (mêmes PDF, déjà téléchargés/cachés) et fournit les agrégats pour la
page (synthèse, évolution mensuelle, répartition indicative par enseigne).
Aucune UI ici.
"""
from __future__ import annotations

import re


def _num(s: str) -> float:
    return float(s.replace(" ", "").replace(" ", "").replace(",", "."))


def lire_stockage(path) -> dict | None:
    """Extrait la prestation de stockage d'un PDF SOFRIPA.

    Retourne {"periode": str|None, "ht": float, "tva": float, "ttc": float}
    ou None si ce n'est pas une facture de stockage (les factures transport
    « 003 » ne contiennent pas le mot STOCKAGE — vérifié sur données réelles).
    """
    import pdfplumber  # import local : garde le module léger à l'import

    txt = ""
    with pdfplumber.open(path) as pdf:
        for pg in pdf.pages:
            txt += (pg.extract_text() or "") + "\n"
    if "STOCKAGE" not in txt.upper():
        return None
    # Libellé de période : "... PERIODE DU 1ER AU 30 AVRIL 2026 ..."
    m_per = re.search(r"PERIODE DU\s+([A-ZÀ-Ü0-9 ]+?\d{4})", txt.upper())
    periode = m_per.group(1).strip().title() if m_per else None
    # Ligne de totaux : "10 116,17 20,00 2 023,23 12 139,40 EUR" (HT, taux, TVA, TTC)
    m_tot = re.search(
        r"([\d  ]+,\d{2})\s+\d{1,2},\d{2}\s+([\d  ]+,\d{2})\s+([\d  ]+,\d{2})\s*EUR",
        txt,
    )
    if not m_tot:
        return None
    return {
        "periode": periode,
        "ht": _num(m_tot.group(1)),
        "tva": _num(m_tot.group(2)),
        "ttc": _num(m_tot.group(3)),
    }


def synthese_stockage(stocks: list[dict], kpis_transport: dict | None = None) -> dict:
    """KPIs stockage sur la période ; croisés avec le transport si fourni.

    stocks : liste de dicts {periode, ht, tva, ttc, date} (une entrée par facture
    mensuelle trouvée sur la période sélectionnée).
    """
    total_ht = sum(s["ht"] for s in stocks)
    total_ttc = sum(s["ttc"] for s in stocks)
    nb = len(stocks)
    out = {
        "nb_factures": nb,
        "total_ht": round(total_ht, 2),
        "total_ttc": round(total_ttc, 2),
        "moyenne_mensuelle_ht": round(total_ht / nb, 2) if nb else None,
    }
    if kpis_transport:
        transport = kpis_transport.get("cout_transport_total_eur") or 0
        logistique = total_ht + transport
        out["cout_transport_ht"] = round(transport, 2)
        out["cout_logistique_ht"] = round(logistique, 2)
        out["part_stockage"] = (total_ht / logistique) if logistique else None
        poids = kpis_transport.get("poids_sofripa_comparable_kg")
        out["stockage_par_kg"] = round(total_ht / poids, 3) if poids else None
        nb_liv = kpis_transport.get("livraisons_appariees")
        out["stockage_par_livraison"] = round(total_ht / nb_liv, 2) if nb_liv else None
        ca = kpis_transport.get("montant_ht_total_eur")
        out["part_stockage_sur_ca"] = (total_ht / ca) if ca else None
    return out


def repartition_par_enseigne(total_stockage_ht: float, par_enseigne: list) -> list[dict]:
    """Répartition INDICATIVE du stockage par enseigne, au prorata du poids expédié.

    Le stockage est facturé globalement (une ligne par mois) : cette ventilation
    est un modèle d'allocation, pas une donnée facturée — à présenter comme telle.
    """
    total_poids = sum(g.poids_sofripa for g in par_enseigne)
    if not total_poids or not total_stockage_ht:
        return []
    rows = [
        {
            "enseigne": g.enseigne,
            "poids": g.poids_sofripa,
            "part": g.poids_sofripa / total_poids,
            "alloue": total_stockage_ht * g.poids_sofripa / total_poids,
        }
        for g in par_enseigne
    ]
    rows.sort(key=lambda r: -r["alloue"])
    return rows
