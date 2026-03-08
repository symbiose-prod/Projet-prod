"""
common/easybeer/suppliers.py
=============================
Supplier (fournisseur) management endpoints + file download.
"""
from __future__ import annotations

import base64 as _b64
from typing import Any

import requests as _requests

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


# ─── File download ─────────────────────────────────────────────────────────

def get_fournisseur_edition(id_fournisseur: int) -> dict[str, Any]:
    """GET /parametres/fournisseur/edition/{id} -> Full supplier data (incl. fichiers)."""
    ep = f"parametres/fournisseur/edition/{id_fournisseur}"
    r = get_session().get(
        f"{BASE}/{ep}",
        auth=_auth(),
        timeout=TIMEOUT,
    )
    _check_response(r, ep)
    return _safe_json(r, ep)


def get_supplier_files(fournisseur: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract file metadata from a ModeleFournisseur dict.

    Returns list of dicts with keys: id, token, nom, mimeType, taille, commentaire.
    Only returns PDF files (most useful for reference extraction).
    """
    fichiers = fournisseur.get("fichiers") or []
    return [
        f for f in fichiers
        if (f.get("mimeType") or "").lower() == "application/pdf"
    ]


def download_supplier_file(file_info: dict[str, Any]) -> bytes | None:
    """Download file content from a ModeleUpload object.

    Tries multiple strategies:
    1. Inline `data` field (base64) if populated
    2. `uri` field as direct URL
    3. Token-based download via known endpoints

    Returns raw bytes or None if download fails.
    """
    # Strategy 1: inline data (base64)
    data_b64 = file_info.get("data")
    if data_b64:
        try:
            return _b64.b64decode(data_b64)
        except Exception:
            _log.debug("Failed to decode inline base64 data for file %s", file_info.get("nom"))

    token = file_info.get("token")
    file_id = file_info.get("id")

    # Strategy 2: uri as a direct downloadable URL
    uri = file_info.get("uri") or file_info.get("urlExterne")
    if uri:
        try:
            url = uri if uri.startswith("http") else f"{BASE}/{uri.lstrip('/')}"
            r = get_session().get(url, auth=_auth(), timeout=TIMEOUT)
            if r.ok and len(r.content) > 100:
                _log.info("Downloaded file via uri: %s (%d bytes)", file_info.get("nom"), len(r.content))
                return r.content
        except Exception as e:
            _log.debug("uri download failed for %s: %s", file_info.get("nom"), e)

    # Strategy 3: token-based download (multiple patterns)
    if token:
        token_endpoints = [
            f"parametres/fournisseur/fichier/telecharger/{token}",
            f"document/telecharger/{token}",
            f"heybilly/document/telecharger/{token}",
        ]
        for ep in token_endpoints:
            try:
                r = get_session().get(
                    f"{BASE}/{ep}",
                    auth=_auth(),
                    timeout=TIMEOUT,
                )
                if r.ok and len(r.content) > 100:
                    _log.info(
                        "Downloaded file via %s: %s (%d bytes)",
                        ep, file_info.get("nom"), len(r.content),
                    )
                    return r.content
            except Exception as e:
                _log.debug("Token endpoint %s failed: %s", ep, e)

    _log.warning(
        "Could not download supplier file: %s (id=%s, token=%s)",
        file_info.get("nom"), file_id, token,
    )
    return None


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text content from a PDF file (requires pypdf).

    Returns extracted text or empty string on failure.
    """
    try:
        import pypdf
    except ImportError:
        _log.warning("pypdf not installed — cannot extract PDF text")
        return ""

    try:
        import io
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text.strip())
        full_text = "\n\n".join(pages_text)
        _log.info("Extracted %d chars from PDF (%d pages)", len(full_text), len(reader.pages))
        return full_text
    except Exception as e:
        _log.warning("PDF text extraction failed: %s", e)
        return ""


def get_supplier_reference_texts(fournisseur: dict[str, Any]) -> list[dict[str, str]]:
    """Download and extract text from all PDF files attached to a supplier.

    Returns list of dicts: [{"filename": "...", "text": "..."}, ...]
    Only includes files where text extraction succeeded.
    """
    files = get_supplier_files(fournisseur)
    if not files:
        _log.debug("No PDF files for supplier %s", fournisseur.get("nom"))
        return []

    results: list[dict[str, str]] = []
    for f in files[:3]:  # limit to 3 files max to avoid slowness
        content = download_supplier_file(f)
        if not content:
            continue
        text = extract_text_from_pdf(content)
        if text and len(text) > 20:
            results.append({
                "filename": f.get("nom", "unknown.pdf"),
                "text": text[:5000],  # limit per file to avoid huge prompts
                "type": (f.get("type") or {}).get("libelle", "Document"),
            })

    _log.info(
        "Got %d reference texts for supplier %s",
        len(results), fournisseur.get("nom"),
    )
    return results
