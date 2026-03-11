"""
common/ai_order.py
==================
AI-powered stock analysis and order proposal using Claude tool_use.

The AI receives:
  - Current stock data (levels, daily consumption, autonomy)
  - Supplier-specific instructions from /ressources (free-form text)
  - Lead time and today's date

It responds with:
  - Natural language analysis and reasoning
  - A structured order proposal via the ``propose_order`` tool

The structured output is then bridged to the existing ``OrderRecommendation``
dataclass to feed the PDF/email pipeline unchanged.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Any

_log = logging.getLogger("ferment.ai_order")


# ─── Config ──────────────────────────────────────────────────────────────────

def _get_api_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "")


_MODEL = "claude-sonnet-4-20250514"

_SYSTEM_PROMPT = """\
Tu es l'assistant de Ferment Station, une brasserie artisanale de kéfir \
et boissons fermentées bio, basée à Ivry-sur-Seine (94200).

Ton rôle : analyser les données de stock d'un fournisseur et proposer \
une commande optimale.

## Données disponibles

Tu reçois pour chaque référence du fournisseur :
- Stock actuel (quantité + unité)
- Consommation journalière moyenne
- Autonomie en jours (stock / conso journalière)
- Seuil bas configuré dans l'ERP

Tu reçois aussi :
- Le délai de livraison du fournisseur
- La date du jour
- Les instructions de commande spécifiques au fournisseur \
  (conditionnement, minimums, conditions particulières)

## Règles

1. Analyse les niveaux de stock et identifie les urgences \
   (autonomie < délai de livraison = critique)
2. Respecte scrupuleusement les instructions du fournisseur \
   (minimums, conditionnement, contraintes)
3. Propose une commande via l'outil `propose_order` avec des quantités \
   cohérentes par rapport aux conditionnements décrits dans les instructions
4. Explique ton raisonnement de manière concise en français
5. Si le stock est largement suffisant (autonomie > 2× délai), \
   indique-le clairement et propose quand même une commande préventive \
   si pertinent
6. Prends en compte les minimums de commande globaux et par référence
7. Arrondis toujours les quantités aux unités de conditionnement entières \
   (palettes complètes, cartons complets, etc.)

## Format

Réponds d'abord en texte (analyse concise), puis utilise l'outil \
`propose_order` pour structurer ta proposition.
"""


# ─── Tool schema ─────────────────────────────────────────────────────────────

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
        "\n\nAnalyse ces données et propose une commande adaptée "
        "en utilisant l'outil `propose_order`."
    )

    return "\n".join(lines)


# ─── Main API call ──────────────────────────────────────────────────────────

def analyze_stock_and_propose_order(
    context_prompt: str,
    conversation: list[dict] | None = None,
) -> dict[str, Any]:
    """Analyze stock data and propose an order using Claude tool_use.

    Args:
        context_prompt: Built by build_stock_context() for initial call.
        conversation: Previous messages for refinement (subsequent calls).

    Returns:
        {
            "text": "Natural language analysis...",
            "order": {propose_order tool input} or None,
            "conversation": updated conversation list for follow-up,
            "tool_use_id": str or None (needed for conversation continuation),
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
        "AI order analysis: %d message(s), model=%s",
        len(messages),
        _MODEL,
    )

    response = client.messages.create(
        model=_MODEL,
        max_tokens=2000,
        system=_SYSTEM_PROMPT,
        tools=[ORDER_PROPOSAL_TOOL],
        messages=messages,
    )

    # Parse response: extract text blocks and tool_use blocks
    text_parts: list[str] = []
    order_data: dict | None = None
    tool_use_id: str | None = None

    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use" and block.name == "propose_order":
            order_data = block.input
            tool_use_id = block.id

    text = "\n".join(text_parts)

    _log.info(
        "AI order response: %d chars text, order=%s, usage=%s",
        len(text),
        "yes" if order_data else "no",
        response.usage,
    )

    # Build conversation for follow-up
    new_conversation = list(messages)
    # Add assistant response (raw content blocks for proper tool_use flow)
    new_conversation.append({
        "role": "assistant",
        "content": response.content,
    })
    # If tool was called, add tool_result to allow continued conversation
    if tool_use_id:
        new_conversation.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "Commande bien reçue. L'utilisateur peut demander des modifications.",
                }
            ],
        })

    return {
        "text": text,
        "order": order_data,
        "conversation": new_conversation,
        "tool_use_id": tool_use_id,
    }


# ─── Bridge to OrderRecommendation ─────────────────────────────────────────

def ai_order_to_recommendation(
    tool_result: dict[str, Any],
    supplier_name: str,
    lead_time_days: int,
) -> "OrderRecommendation":
    """Convert AI tool_use output to an OrderRecommendation dataclass.

    This bridges the AI output to the existing PDF/email pipeline
    (build_bon_commande_pdf, _open_order_dialog, etc.).
    """
    from ui._stocks_calc import OrderItem, OrderRecommendation

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
