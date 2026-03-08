"""
common/ai.py
============
Claude AI client for order email generation.

Uses the Anthropic Python SDK.  Feature is disabled if ANTHROPIC_API_KEY is not
set — ``is_ai_configured()`` returns False and the UI hides the chat button.
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

Ton rôle : rédiger des emails professionnels de commande fournisseur en français.

Règles :
- Ton professionnel mais cordial, vouvoiement obligatoire
- Inclure systématiquement : références exactes, quantités (palettes + unités), \
  date de livraison souhaitée
- Signature : "Cordialement,\\nFerment Station"
- Format : HTML simple (balises <p>, <ul>, <li>, <strong> uniquement)
- La PREMIÈRE LIGNE de ta réponse doit être l'objet du mail, préfixé par \
  "Objet : " (ex: "Objet : Commande bouteilles 33cl et 75cl — Ferment Station")
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
            - items: list[dict] with label, suggested_pallets, suggested_qty,
              coverage_days, bottles_per_pallet
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
    items_text = "\n".join(
        f"  - {it['label']}: {it['suggested_pallets']} palette(s), "
        f"{it['suggested_qty']:,} unités".replace(",", "\u202f")
        + (f" ({it['bottles_per_pallet']}/palette)"
           if it.get("bottles_per_pallet") else "")
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

    return f"""\
Rédige un email de commande pour le fournisseur suivant :

Fournisseur : {context.get('supplier_name', '?')}
Situation : {urgency_text}
Délai de livraison habituel : {context.get('lead_time_days', '?')} jours
Date limite de commande : {context.get('order_deadline', 'Non définie')}

Articles à commander :
{items_text}

Rédige l'email en HTML. Première ligne = "Objet : ..." puis le corps du mail.
Demande une confirmation de commande et une date de livraison prévisionnelle.
"""
