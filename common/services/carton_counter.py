"""
common/services/carton_counter.py
=================================
PoC : compte les cartons sur une photo de palette via Claude Vision.

Objectif business : éliminer les erreurs de calcul mental quand
l'opérateur compte des cartons en haut d'une palette partielle ou les
cartons « extras » au-dessus d'une palette pleine. L'opérateur prend une
photo, Claude répond avec un total + confiance, l'opérateur confirme ou
corrige via les boutons +/- existants.

Pas d'intégration dans le flow nominal pour l'instant — utilisé via la
page admin /test-carton-counter pour valider la fiabilité sur des photos
réelles avant d'envisager l'intégration dans /etiquettes-palette.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass

_log = logging.getLogger("ferment.carton_counter")


# Modèle vision-capable. Sonnet 4.6 = bon équilibre coût/qualité pour
# du comptage d'objets régulièrement disposés. Haiku 4.5 serait moins
# cher mais on évalue d'abord la précision sur le modèle plus capable
# avant d'optimiser le coût.
_MODEL = "claude-sonnet-4-6"

# Limite défensive sur la taille de l'image envoyée à l'API.
# Le JS côté front resize à 1280px max (≈ 200-500 Ko en JPEG 0.85).
_MAX_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class CartonCountResult:
    """Résultat structuré du comptage."""
    count: int                  # nb cartons visibles
    confidence: str             # 'high' | 'medium' | 'low'
    description: str            # 1 phrase d'explication courte
    raw_response: str           # debug — pour analyse en cas de souci


_PROMPT = """Tu regardes une photo prise du dessus d'une palette de cartons \
identiques dans un entrepôt de production.

Ta tâche : compter avec précision le nombre de cartons visibles sur cette \
photo. Ces cartons sont rangés à plat, soit en formant un étage complet \
(motif régulier rectangulaire), soit posés en plus sur le dessus d'une \
palette déjà pleine.

Règles de comptage :
- Compte UNIQUEMENT les cartons clairement visibles depuis le dessus.
- Ne compte PAS les cartons d'un étage inférieur partiellement visible.
- Si tu vois clairement un étage complet (motif régulier sans manque), \
indique-le dans la description.
- Si la photo est floue, mal cadrée, ou ne montre pas une palette de \
cartons, mets ``confidence`` à ``low`` et explique-le dans la description.

Réponds STRICTEMENT en JSON valide, sans markdown, sans texte avant ni \
après. Schéma exact :
{
  "count": <nombre entier de cartons visibles>,
  "confidence": "high" | "medium" | "low",
  "description": "<une phrase brève décrivant ce que tu vois>"
}"""


def count_cartons_in_photo(
    image_bytes: bytes,
    *,
    media_type: str = "image/jpeg",
) -> CartonCountResult:
    """Demande à Claude Vision de compter les cartons sur la photo.

    Args:
        image_bytes: bytes de l'image (JPEG/PNG/WebP/GIF).
        media_type: MIME type pour l'API Anthropic. Défaut JPEG.
            Tout autre type est remappé sur JPEG pour ne pas casser
            l'appel (l'API n'accepte qu'un set restreint).

    Raises:
        RuntimeError: si ``ANTHROPIC_API_KEY`` n'est pas configurée ou
            si l'image dépasse la limite de taille défensive.
        ValueError: si la réponse du modèle n'est pas un JSON parsable.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY manquante — configure-la dans .env "
            "pour activer le comptage par vision.",
        )

    if len(image_bytes) == 0:
        raise ValueError("Image vide")
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise RuntimeError(
            f"Image trop volumineuse ({len(image_bytes) // 1024} Ko, "
            f"max {_MAX_IMAGE_BYTES // 1024} Ko)",
        )

    # MIME : l'API n'accepte que image/jpeg, image/png, image/gif, image/webp.
    # Tout autre type (image/heic souvent) → on tente JPEG (le front
    # resize en JPEG de toute façon).
    api_media_type = media_type if media_type in (
        "image/jpeg", "image/png", "image/gif", "image/webp",
    ) else "image/jpeg"

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    msg = client.messages.create(
        model=_MODEL,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": api_media_type,
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": _PROMPT},
            ],
        }],
    )

    raw = "".join(
        b.text for b in msg.content if hasattr(b, "text")
    ).strip()
    _log.info(
        "Carton count : %d Ko envoyés, réponse %d chars",
        len(image_bytes) // 1024, len(raw),
    )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning("Réponse non JSON parsable : %r", raw[:300])
        raise ValueError(
            f"Réponse Claude non parsable comme JSON : {exc}",
        ) from exc

    try:
        count = int(data.get("count", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Champ 'count' invalide : {data.get('count')!r}") from exc

    confidence = str(data.get("confidence", "low")).lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"

    return CartonCountResult(
        count=count,
        confidence=confidence,
        description=str(data.get("description", "")).strip(),
        raw_response=raw,
    )
