"""
common/ai.py
============
DEPRECATED — email generation is now handled by ``common/ai_order.py``
via the unified ``analyze_and_respond()`` function with ``draft_order_email`` tool.

Only ``is_ai_configured()`` is still used (checks ANTHROPIC_API_KEY).
``generate_order_email()`` and ``_build_initial_prompt()`` are kept for
backward compatibility but are no longer called from the UI.
"""
from __future__ import annotations

import logging
import os
from typing import Any

_log = logging.getLogger("ferment.ai")


# ─── Config ──────────────────────────────────────────────────────────────────

def _get_api_key() -> str:
    """Lazy env-var loading (same pattern as common/email.py)."""
    return os.getenv("ANTHROPIC_API_KEY", "")


def is_ai_configured() -> bool:
    """True if Claude AI is available (ANTHROPIC_API_KEY is set)."""
    return bool(_get_api_key())


_MODEL = "claude-sonnet-4-20250514"

_SYSTEM_PROMPT = """\
Tu es l'assistant de Ferment Station, une brasserie artisanale de kéfir et \
boissons fermentées bio, basée à Ivry-sur-Seine (94200).

Ton rôle : rédiger des emails professionnels de commande fournisseur.

Règles :
- La LANGUE du mail est spécifiée dans le contexte (français ou anglais). \
  Adapte tout le contenu (objet, corps, signature) à la langue demandée.
- En français : vouvoiement obligatoire. En anglais : ton formel "Dear…"
- Ton professionnel mais cordial
- Inclure systématiquement : références exactes, quantités (conditionnement + unités), \
  date de livraison souhaitée
- Si des DOCUMENTS DE RÉFÉRENCE fournisseur sont fournis (confirmations de commande, \
  factures, bons de livraison passés), utilise les références produits, numéros \
  d'article, codes et formats exacts trouvés dans ces documents. \
  Cela rend la commande plus précise et facilite le traitement côté fournisseur.
- Signature : "Cordialement,\\nFerment Station" (FR) ou \
  "Kind regards,\\nFerment Station" (EN)
- Format : HTML simple (balises <p>, <ul>, <li>, <strong> uniquement)
- La PREMIÈRE LIGNE de ta réponse doit être l'objet du mail, préfixé par \
  "Objet : " (FR) ou "Subject: " (EN)
- Le reste de la réponse est le corps HTML du mail
- Ne jamais inventer de prix ou de conditions non fournies dans le contexte
- Ne pas inclure d'en-tête "De:" ou "À:" — uniquement l'objet puis le corps
"""


# ─── Main entry-point ────────────────────────────────────────────────────────

def generate_order_email(
    context: dict[str, Any],
    conversation: list[dict[str, str]] | None = None,
) -> str:
    """Generate or refine an order email draft using Claude.

    Args:
        context: Order context with keys:
            - supplier_name: str
            - supplier_email: str | None
            - items: list[dict] with label, suggested_units, suggested_qty,
              coverage_days, qty_per_unit
            - lead_time_days: int
            - order_deadline: str | None (formatted date)
            - urgency: str ("critical" | "warning" | "ok")
        conversation: Previous messages for refinement
            [{"role": "user"/"assistant", "content": ...}]
            If provided, ``context`` is ignored (already in conversation history).

    Returns:
        Full response text: first line = "Objet : ...", rest = HTML body.

    Raises:
        RuntimeError: if ANTHROPIC_API_KEY is not set.
    """
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non configurée")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    # ── Build messages ────────────────────────────────────────────────────
    if conversation:
        messages = list(conversation)
    else:
        messages = [{"role": "user", "content": _build_initial_prompt(context)}]

    _log.info(
        "Claude API call: %d message(s), model=%s",
        len(messages),
        _MODEL,
    )

    response = client.messages.create(
        model=_MODEL,
        max_tokens=2000,
        system=_SYSTEM_PROMPT,
        messages=messages,
    )

    text = response.content[0].text
    _log.info(
        "Claude response: %d chars, usage=%s",
        len(text),
        response.usage,
    )
    return text


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _build_initial_prompt(context: dict[str, Any]) -> str:
    """Build the first user message from order context."""
    items = context.get("items") or []
    order_unit = context.get("order_unit", "palette")
    qty_unit = context.get("qty_unit", "unités")
    items_text = "\n".join(
        f"  - {it['label']}: {it['suggested_units']} {order_unit}(s), "
        f"{it['suggested_qty']:,} {qty_unit}".replace(",", "\u202f")
        + (f" ({it['qty_per_unit']}/{order_unit})"
           if it.get("qty_per_unit") else "")
        + (f" — couverture ~{it['coverage_days']:.0f} jours"
           if it.get("coverage_days") else "")
        for it in items
    )

    urgency_map = {
        "critical": "URGENTE — stock insuffisant pour couvrir le délai de livraison",
        "warning": "À planifier rapidement — stock limité",
        "ok": "Commande de réapprovisionnement standard",
    }
    urgency_text = urgency_map.get(context.get("urgency", "ok"), "Standard")

    # Language
    lang = context.get("language", "fr")
    lang_label = "français" if lang == "fr" else "anglais"

    # Delivery preference
    delivery_pref = context.get("delivery_preference", "asap")
    if delivery_pref == "asap":
        delivery_text = "Dès que possible (ASAP)" if lang == "fr" else "As soon as possible (ASAP)"
    else:
        delivery_text = context.get("delivery_date_requested", delivery_pref)

    # Supplier reference documents (extracted from EasyBeer files)
    ref_texts = context.get("supplier_references") or []
    ref_section = ""
    if ref_texts:
        ref_parts = []
        for ref in ref_texts:
            ref_parts.append(
                f"--- {ref.get('type', 'Document')} : {ref['filename']} ---\n"
                f"{ref['text']}"
            )
        ref_section = (
            "\n\nDOCUMENTS DE RÉFÉRENCE du fournisseur (commandes/factures passées) :\n"
            "Utilise les références produits, codes articles et formats exacts "
            "trouvés ci-dessous pour rédiger une commande précise.\n\n"
            + "\n\n".join(ref_parts)
        )

    return f"""\
Rédige un email de commande pour le fournisseur suivant :

Langue du mail : {lang_label}
Fournisseur : {context.get('supplier_name', '?')}
Situation : {urgency_text}
Délai de livraison habituel : {context.get('lead_time_days', '?')} jours
Date limite de commande : {context.get('order_deadline', 'Non définie')}
Date de livraison souhaitée : {delivery_text}

Articles à commander :
{items_text}
{ref_section}

Rédige l'email en {lang_label}, en HTML. \
Première ligne = "{'Objet' if lang == 'fr' else 'Subject'} : ..." puis le corps du mail.
Demande une confirmation de commande et une date de livraison prévisionnelle.
"""
