"""
common/ai_order.py
==================
Unified AI for stock analysis, order proposals **and** email drafting.

Uses Claude tool_use with two tools:
  - ``propose_order`` — structured order proposal
  - ``draft_order_email`` — order email in HTML

A single conversation thread flows from the inline stock-analysis chat
through to the email-drafting dialog, preserving full context.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pages._stocks_calc import OrderRecommendation

_log = logging.getLogger("ferment.ai_order")


# ─── Config ──────────────────────────────────────────────────────────────────

def _get_api_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "")


def is_ai_configured() -> bool:
    """True si Claude AI est disponible (ANTHROPIC_API_KEY set)."""
    return bool(_get_api_key())


_MODEL = "claude-sonnet-4-20250514"

_SYSTEM_PROMPT = """\
Tu es l'assistant approvisionnement de Ferment Station.

## Principe fondamental

Les **instructions de commande fournisseur** (section « Instructions de \
commande fournisseur » dans le message utilisateur) sont ta seule source \
de vérité pour la logique métier. Suis-les à la lettre : seuils, \
minimums, conditionnement, et surtout conditions pour NE PAS commander.

## Analyse de stock

1. Applique les règles des instructions fournisseur pour décider s'il \
   faut commander ou non.
2. Si les instructions concluent qu'il ne faut pas commander : \
   ne propose PAS de commande, n'utilise PAS l'outil `propose_order`. \
   Dis clairement qu'il n'y a pas besoin de commander et quand réévaluer.
3. Si une commande est justifiée : utilise l'outil `propose_order`.
4. Explique ton raisonnement de manière concise en français.

## Rédaction d'email

Quand on te demande de rédiger l'email de commande, utilise l'outil \
`draft_order_email` :
1. Langue spécifiée par l'utilisateur (français ou anglais).
2. Français : vouvoiement, « Cordialement ». \
   Anglais : « Dear… », « Kind regards ».
3. Inclure : références exactes, quantités, date de livraison souhaitée.
4. Si des documents de référence fournisseur sont fournis, utilise les \
   références produits et codes exacts de ces documents.
5. Format HTML simple : <p>, <ul>, <li>, <strong> uniquement.
6. NE PAS inclure d'en-tête « De: » ou « À: ». \
   La signature complète est ajoutée automatiquement.
7. Ne jamais inventer de prix ou conditions non fournis.
8. Demander confirmation de commande et date de livraison prévisionnelle.

## Format de réponse

Texte d'analyse concis d'abord, puis outil (`propose_order` ou \
`draft_order_email`) si applicable.
"""


# ─── Tool schemas ────────────────────────────────────────────────────────────

ORDER_PROPOSAL_TOOL = {
    "name": "propose_order",
    "description": (
        "Propose une commande fournisseur structurée. "
        "Utilise cet outil pour formaliser ta proposition de commande "
        "après ton analyse textuelle."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "Lignes de commande",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "description": "Nom de la référence (ex: 'Bouteille 33cl')",
                        },
                        "units": {
                            "type": "integer",
                            "description": "Nombre d'unités de commande (palettes, cartons, bidons...)",
                        },
                        "qty": {
                            "type": "integer",
                            "description": "Quantité totale en unité de base (unités, kg, capsules...)",
                        },
                        "conditionnement": {
                            "type": "string",
                            "description": "Info conditionnement (ex: '3610/palette', '25 kg/bidon')",
                        },
                        "coverage_days": {
                            "type": "number",
                            "description": "Nombre de jours de couverture estimé",
                        },
                    },
                    "required": ["label", "units", "qty"],
                },
            },
            "order_unit": {
                "type": "string",
                "description": "Unité de commande (palette, carton, bidon, lot...)",
            },
            "qty_unit": {
                "type": "string",
                "description": "Unité de quantité (unités, kg, capsules...)",
            },
            "urgency": {
                "type": "string",
                "enum": ["critical", "warning", "ok"],
                "description": (
                    "critical = stock < délai livraison, "
                    "warning = stock < 2× délai, "
                    "ok = stock confortable"
                ),
            },
            "delivery_suggestion": {
                "type": "string",
                "description": "Suggestion de date ou mode de livraison",
            },
            "reasoning": {
                "type": "string",
                "description": "Résumé court du raisonnement (1-2 phrases)",
            },
        },
        "required": ["items", "order_unit", "qty_unit", "urgency"],
    },
}


DRAFT_EMAIL_TOOL = {
    "name": "draft_order_email",
    "description": (
        "Rédige un email de commande fournisseur en HTML. "
        "Utilise cet outil pour formaliser l'email après la proposition "
        "de commande. Ne pas inclure d'en-tête De:/À: ni la signature "
        "complète (elle est ajoutée automatiquement)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": (
                    "Objet de l'email "
                    "(ex: 'Commande Ferment Station — Bouteilles 33cl')"
                ),
            },
            "html_body": {
                "type": "string",
                "description": (
                    "Corps de l'email en HTML simple "
                    "(balises <p>, <ul>, <li>, <strong> uniquement). "
                    "Terminer par la formule de politesse courte."
                ),
            },
        },
        "required": ["subject", "html_body"],
    },
}

_ALL_TOOLS = [ORDER_PROPOSAL_TOOL, DRAFT_EMAIL_TOOL]


# ─── Context builder ────────────────────────────────────────────────────────

def build_stock_context(
    supplier_name: str,
    lead_time_days: int,
    ai_instructions: str,
    items: list[dict[str, Any]],
    window_days: int,
) -> str:
    """Build the user prompt with stock data and supplier instructions.

    Args:
        items: list of dicts with keys:
            label, current_stock, unit, seuil_bas, daily_consumption,
            stock_days, consumption (total over window)
    """
    today = date.today()

    lines = [
        f"Date du jour : {today.strftime('%d/%m/%Y')}",
        f"Fournisseur : {supplier_name}",
        f"Délai de livraison : {lead_time_days} jours",
        f"Fenêtre d'analyse : {window_days} jours",
        "",
    ]

    # Stock data per item
    lines.append("## Données de stock\n")
    for it in items:
        stock_days_str = (
            f"{it['stock_days']:.0f} jours"
            if it.get("stock_days") is not None
            else "N/A (pas de consommation)"
        )
        lines.append(
            f"- **{it['label']}** : "
            f"stock {it['current_stock']:,.0f} {it['unit']}, "
            f"conso {it['daily_consumption']:.1f} {it['unit']}/jour, "
            f"autonomie {stock_days_str}, "
            f"seuil bas {it['seuil_bas']:,.0f} {it['unit']}"
        )

    # Supplier instructions
    if ai_instructions:
        lines.append("\n## Instructions de commande fournisseur\n")
        lines.append(ai_instructions)

    lines.append(
        "\n\nAnalyse ces données en suivant les instructions fournisseur ci-dessus. "
        "Si une commande est justifiée, utilise l'outil `propose_order`. "
        "Sinon, dis clairement qu'il n'y a pas besoin de commander."
    )

    return "\n".join(lines)


# ─── Main API call ──────────────────────────────────────────────────────────

def analyze_and_respond(
    context_prompt: str,
    conversation: list[dict] | None = None,
) -> dict[str, Any]:
    """Unified AI call: analyse stock, propose orders, or draft emails.

    This single function handles the full conversation lifecycle.
    On the first call it analyses stock data and proposes an order.
    On subsequent calls it can refine the order or draft an email.

    Args:
        context_prompt: Built by build_stock_context() for initial call.
        conversation: Previous messages for refinement (subsequent calls).

    Returns:
        {
            "text": "Natural language analysis...",
            "order": {propose_order tool input} or None,
            "email": {"subject": "...", "html_body": "..."} or None,
            "conversation": updated conversation list for follow-up,
            "tool_use_id": str or None,
        }
    """
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non configurée")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    # Build messages
    if conversation:
        messages = list(conversation)
    else:
        messages = [{"role": "user", "content": context_prompt}]

    _log.info(
        "AI call: %d message(s), model=%s",
        len(messages),
        _MODEL,
    )

    # Prompt caching: system prompt + tools sont statiques → cache ephemeral
    # pour économiser 30-50% du coût sur les appels suivants (TTL 5 min).
    # https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
    system_blocks = [
        {
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # cache_control sur le dernier tool → marque tous les tools comme cachés
    tools_cached = [
        *_ALL_TOOLS[:-1],
        {**_ALL_TOOLS[-1], "cache_control": {"type": "ephemeral"}},
    ]

    response = client.messages.create(
        model=_MODEL,
        max_tokens=4000,
        system=system_blocks,
        tools=tools_cached,
        messages=messages,
    )

    # Parse response: extract text blocks and tool_use blocks
    text_parts: list[str] = []
    order_data: dict | None = None
    email_data: dict | None = None
    tool_results: list[dict] = []

    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            if block.name == "propose_order":
                order_data = block.input
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": (
                        "Commande bien reçue. "
                        "L'utilisateur peut demander des modifications."
                    ),
                })
            elif block.name == "draft_order_email":
                email_data = block.input
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": (
                        "Email bien reçu. "
                        "L'utilisateur peut demander des modifications."
                    ),
                })

    text = "\n".join(text_parts)

    # Pick last tool_use_id for backward compat
    tool_use_id: str | None = None
    for block in response.content:
        if block.type == "tool_use":
            tool_use_id = block.id

    # Log enrichi avec stats de cache (cache_read_input_tokens = tokens économisés)
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_created = getattr(usage, "cache_creation_input_tokens", 0) or 0
    _log.info(
        "AI response: %d chars, order=%s, email=%s, in=%d out=%d cache_read=%d cache_created=%d",
        len(text),
        "yes" if order_data else "no",
        "yes" if email_data else "no",
        getattr(usage, "input_tokens", 0),
        getattr(usage, "output_tokens", 0),
        cache_read,
        cache_created,
    )

    # Build conversation for follow-up
    new_conversation = list(messages)
    new_conversation.append({
        "role": "assistant",
        "content": response.content,
    })
    # All tool_result entries go in one user message
    if tool_results:
        new_conversation.append({
            "role": "user",
            "content": tool_results,
        })

    return {
        "text": text,
        "order": order_data,
        "email": email_data,
        "conversation": new_conversation,
        "tool_use_id": tool_use_id,
    }


# Keep old name as alias for backward compatibility
analyze_stock_and_propose_order = analyze_and_respond


# ─── Bridge to OrderRecommendation ─────────────────────────────────────────

def ai_order_to_recommendation(
    tool_result: dict[str, Any],
    supplier_name: str,
    lead_time_days: int,
) -> OrderRecommendation:
    """Convert AI tool_use output to an OrderRecommendation dataclass.

    This bridges the AI output to the existing PDF/email pipeline
    (build_bon_commande_pdf, _open_order_dialog, etc.).
    """
    from pages._stocks_calc import OrderItem, OrderRecommendation

    items: list[OrderItem] = []
    for item_data in tool_result.get("items", []):
        units = item_data.get("units", 0)
        qty = item_data.get("qty", 0)
        qpu = qty // max(units, 1) if units > 0 else qty

        items.append(OrderItem(
            label=item_data["label"],
            stock_days=None,
            days_before_order=None,
            deadline=None,
            daily_consumption=0,
            qty_per_unit=qpu,
            suggested_units=units,
            suggested_qty=qty,
            coverage_days=item_data.get("coverage_days"),
        ))

    urgency = tool_result.get("urgency", "ok")
    today = date.today()

    # Compute order deadline from urgency
    if urgency == "critical":
        order_deadline = today
    elif urgency == "warning":
        order_deadline = today + timedelta(days=lead_time_days // 2)
    else:
        order_deadline = today + timedelta(days=lead_time_days)

    return OrderRecommendation(
        supplier=supplier_name,
        lead_time_days=lead_time_days,
        min_order=0,  # AI already applied constraints
        can_split=True,
        items=items,
        order_deadline=order_deadline,
        urgency=urgency,
        order_unit=tool_result.get("order_unit", "palette"),
        qty_unit=tool_result.get("qty_unit", "unités"),
    )
