"""
common/easybeer/suppliers.py
=============================
Supplier (fournisseur) management endpoints.
"""
from __future__ import annotations

from typing import Any

from ._client import BASE, TIMEOUT, _auth, _check_response, _log, _safe_json, get_session


def get_all_fournisseurs() -> list[dict[str, Any]]:
    """GET /parametres/fournisseur/liste/all -> Liste complete des fournisseurs."""
    ep = "parametres/fournisseur/liste/all"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    return _safe_json(r, ep)


def find_fournisseur_by_name(name: str) -> dict[str, Any] | None:
    """Find a supplier by name (case-insensitive).

    Returns the best match or None.
    Priority: exact match > partial match (contains).
    """
    fournisseurs = get_all_fournisseurs()
    name_lower = name.strip().lower()

    # Exact match first
    for f in fournisseurs:
        if (f.get("nom") or "").strip().lower() == name_lower:
            return f

    # Partial match fallback
    for f in fournisseurs:
        if name_lower in (f.get("nom") or "").strip().lower():
            return f

    _log.warning("Fournisseur '%s' non trouve dans EasyBeer", name)
    return None


def extract_supplier_email(fournisseur: dict[str, Any]) -> str | None:
    """Extract the best email address from a ModeleFournisseur dict.

    Priority: contactPrincipal.email > contact.email > first contacts[].email
    """
    for key in ("contactPrincipal", "contact"):
        contact = fournisseur.get(key)
        if contact and contact.get("email"):
            return contact["email"].strip()

    for contact in fournisseur.get("contacts") or []:
        if contact.get("email"):
            return contact["email"].strip()

    return None


def extract_supplier_contact_name(fournisseur: dict[str, Any]) -> str | None:
    """Extract the main contact name from a ModeleFournisseur dict."""
    for key in ("contactPrincipal", "contact"):
        contact = fournisseur.get(key)
        if not contact:
            continue
        parts = list(filter(None, [
            contact.get("prenom"),
            contact.get("nom"),
        ]))
        if parts:
            return " ".join(parts)
    return None


def extract_supplier_address(fournisseur: dict[str, Any]) -> list[str]:
    """Extract address lines from a ModeleFournisseur dict."""
    adresse = fournisseur.get("adresse") or {}
    lines: list[str] = []

    nom = fournisseur.get("nom")
    if nom:
        lines.append(nom)

    # ModeleAdresse has a "complete" field = full formatted address
    if adresse.get("complete"):
        lines.append(adresse["complete"])
    else:
        if adresse.get("denomination"):
            lines.append(adresse["denomination"])
        for field in ("ligne1", "ligne2", "ligne3", "ligne4"):
            val = adresse.get(field)
            if val and val.strip():
                lines.append(val.strip())
        if not any(adresse.get(f) for f in ("ligne1", "ligne2", "ligne3", "ligne4")):
            street_parts = list(filter(None, [
                adresse.get("numero"),
                adresse.get("rue"),
            ]))
            if street_parts:
                lines.append(" ".join(street_parts))
        cp_ville = " ".join(filter(None, [
            adresse.get("codePostal"),
            adresse.get("ville"),
        ]))
        if cp_ville:
            lines.append(cp_ville)
        if adresse.get("pays"):
            lines.append(adresse["pays"])

    return lines
