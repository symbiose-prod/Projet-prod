#!/usr/bin/env python3
"""
scripts/migrate_photos_to_s3.py
================================
Script de migration one-shot pour externaliser les photos d'incidents des
fiches production depuis le JSONB PostgreSQL vers OVH Object Storage.

État avant migration (legacy) :
    production_sheets.data.incidents.photos = [
        {"base64": "/9j/4AAQSkZJRgABA...", ...},
        ...
    ]

État après migration :
    production_sheets.data.incidents.photos = [
        {
            "base64": "/9j/4AAQSkZJRgABA...",   # conservé temporairement
            "key": "production/photos/{tenant}/{date}/{sheet}/{uuid}.jpg",
            "content_type": "image/jpeg",
            "size_bytes": 12345,
            "migrated_at": "2026-05-23T13:30:00+00:00",
        },
        ...
    ]

Le ``base64`` est CONSERVÉ pendant la phase de transition (sécurité) — il
sera retiré dans une PR ultérieure avec --remove-base64 une fois que :
1. Le PDF generator sait lire depuis ``key``
2. L'app iOS utilise les URLs signées
3. Quelques jours/semaines de prod ont validé la stabilité

Usage :
    # Mode par défaut : dry-run, ne modifie rien, juste affiche le plan
    python scripts/migrate_photos_to_s3.py

    # Pour de vrai (nécessite OVH_S3_* configurés dans l'env) :
    python scripts/migrate_photos_to_s3.py --apply

    # Filtrer par tenant :
    python scripts/migrate_photos_to_s3.py --apply --tenant-id <uuid>

    # Limite (pratique pour tester sur un petit sous-ensemble) :
    python scripts/migrate_photos_to_s3.py --apply --limit 5

    # Étape finale (à exécuter quand on est confiant) — retire le base64 des
    # photos qui ont déjà une ``key`` (free la place dans le JSONB) :
    python scripts/migrate_photos_to_s3.py --remove-base64 --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Permet d'exécuter depuis n'importe où dans le repo
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Charge .env si présent (compat dev local)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger("migrate_photos")


# ─── Stats / rapport ──────────────────────────────────────────────────────


class Stats:
    """Compteurs cumulés pour le rapport final."""

    def __init__(self) -> None:
        self.sheets_scanned = 0
        self.sheets_with_photos = 0
        self.photos_total = 0
        self.photos_already_migrated = 0
        self.photos_to_migrate = 0
        self.photos_migrated = 0
        self.photos_failed = 0
        self.bytes_migrated = 0
        self.base64_removed = 0

    def print_report(self, *, dry_run: bool) -> None:
        mode = "DRY-RUN" if dry_run else "APPLIED"
        print()
        print("=" * 60)
        print(f"  RAPPORT MIGRATION PHOTOS — mode {mode}")
        print("=" * 60)
        print(f"  Fiches scannées        : {self.sheets_scanned}")
        print(f"  Fiches avec photos     : {self.sheets_with_photos}")
        print(f"  Photos totales         : {self.photos_total}")
        print(f"  Photos déjà migrées    : {self.photos_already_migrated}")
        print(f"  Photos à migrer        : {self.photos_to_migrate}")
        if not dry_run:
            print(f"  Photos migrées OK      : {self.photos_migrated}")
            print(f"  Photos en échec        : {self.photos_failed}")
            mb = self.bytes_migrated / (1024 * 1024)
            print(f"  Volume migré           : {self.bytes_migrated:,} bytes ({mb:.2f} Mo)")
            if self.base64_removed > 0:
                print(f"  Base64 supprimés       : {self.base64_removed}")
        print("=" * 60)


# ─── Helpers ──────────────────────────────────────────────────────────────


def _decode_base64(b64: str) -> bytes | None:
    """Décode une string base64 en bytes. Retourne None si invalide."""
    import base64 as _b64
    try:
        # Supporte les data-URLs (data:image/jpeg;base64,...)
        if "," in b64 and b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]
        return _b64.b64decode(b64, validate=False)
    except Exception:  # noqa: BLE001
        return None


def _detect_content_type(image_bytes: bytes) -> str:
    """Détecte le content-type d'une image via ses magic bytes."""
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image/heic"
    return "image/jpeg"  # fallback raisonnable


# ─── Migration d'une photo ────────────────────────────────────────────────


def _migrate_photo(
    photo: dict[str, Any],
    *,
    tenant_id: str,
    sheet_id: str,
    stats: Stats,
    dry_run: bool,
) -> dict[str, Any]:
    """Migre une photo individuelle. Retourne le dict photo mis à jour.

    Si la photo a déjà une ``key`` non vide, on skip (déjà migrée).
    Si pas de ``base64``, on skip (rien à faire).
    """
    if photo.get("key"):
        stats.photos_already_migrated += 1
        return photo

    b64 = photo.get("base64") or ""
    if not b64:
        return photo

    image_bytes = _decode_base64(b64)
    if image_bytes is None:
        _log.warning("Sheet %s: base64 invalide pour une photo — skip", sheet_id)
        stats.photos_failed += 1
        return photo

    content_type = photo.get("content_type") or _detect_content_type(image_bytes)
    size = len(image_bytes)
    stats.photos_to_migrate += 1

    if dry_run:
        _log.info(
            "  [DRY] Migrerait photo (%s, %d Ko) du sheet %s",
            content_type, size // 1024, sheet_id,
        )
        return photo

    # Apply mode : upload vers S3
    try:
        from common.object_storage import upload_photo
        key = upload_photo(
            image_bytes,
            tenant_id=tenant_id,
            sheet_id=sheet_id,
            content_type=content_type,
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("Sheet %s: upload S3 échec : %s", sheet_id, exc)
        stats.photos_failed += 1
        return photo

    stats.photos_migrated += 1
    stats.bytes_migrated += size

    # Enrichit le dict photo avec la nouvelle key + metadata
    updated = dict(photo)
    updated["key"] = key
    updated["content_type"] = content_type
    updated["size_bytes"] = size
    updated["migrated_at"] = datetime.now(UTC).isoformat()
    # base64 conservé pour l'instant (rétrocompat PDF generator)
    return updated


# ─── Migration d'une fiche ────────────────────────────────────────────────


def _migrate_sheet(
    sheet_row: dict[str, Any],
    *,
    stats: Stats,
    dry_run: bool,
    remove_base64: bool,
) -> dict[str, Any] | None:
    """Migre toutes les photos d'incident d'une fiche.

    Retourne le nouveau ``data`` à persister (dict) ou None si rien à changer.
    """
    sheet_id = str(sheet_row["id"])
    tenant_id = str(sheet_row["tenant_id"])
    data = sheet_row.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None

    incidents = data.get("incidents") or {}
    photos = incidents.get("photos") or []
    if not photos:
        return None

    stats.sheets_with_photos += 1
    stats.photos_total += len(photos)

    new_photos: list[dict[str, Any]] = []
    changed = False
    for photo in photos:
        if not isinstance(photo, dict):
            new_photos.append(photo)
            continue
        original = dict(photo)
        updated = _migrate_photo(
            photo,
            tenant_id=tenant_id,
            sheet_id=sheet_id,
            stats=stats,
            dry_run=dry_run,
        )

        # Phase finale : si --remove-base64 et la photo a déjà une key,
        # on supprime le base64 pour libérer la place.
        if remove_base64 and updated.get("key") and updated.get("base64"):
            if not dry_run:
                updated.pop("base64", None)
                stats.base64_removed += 1
            else:
                _log.info(
                    "  [DRY] Retirerait base64 (%d Ko) du sheet %s (key=%s)",
                    len(updated.get("base64", "")) // 1024,
                    sheet_id,
                    updated["key"][-30:],
                )
                stats.base64_removed += 1

        if updated != original:
            changed = True
        new_photos.append(updated)

    if not changed:
        return None

    new_data = dict(data)
    new_incidents = dict(incidents)
    new_incidents["photos"] = new_photos
    new_data["incidents"] = new_incidents
    return new_data


# ─── Main loop ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="Effectue la migration pour de vrai. Sans ce flag = dry-run.",
    )
    parser.add_argument(
        "--remove-base64", action="store_true",
        help="Phase finale : retire le base64 des photos qui ont déjà une key.",
    )
    parser.add_argument(
        "--tenant-id", type=str, default=None,
        help="Limite à un tenant (UUID). Sans : tous les tenants.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Nombre max de fiches à traiter (pour tester). Sans : illimité.",
    )
    args = parser.parse_args(argv)

    dry_run = not args.apply

    # Précheck OVH config si --apply
    if not dry_run and not args.remove_base64:
        from common.object_storage import is_configured
        if not is_configured():
            _log.error(
                "OVH S3 non configuré — définir OVH_S3_ENDPOINT, OVH_S3_BUCKET, "
                "OVH_S3_ACCESS_KEY, OVH_S3_SECRET_KEY dans l'env (ou .env)",
            )
            return 1

    from db.conn import run_sql

    where = "WHERE data ? 'incidents'"
    params: dict[str, Any] = {}
    if args.tenant_id:
        where += " AND tenant_id = :tid"
        params["tid"] = args.tenant_id

    limit_clause = f"LIMIT {int(args.limit)}" if args.limit else ""

    sql = f"""
        SELECT id, tenant_id, data
        FROM production_sheets
        {where}
        ORDER BY created_at DESC
        {limit_clause}
    """

    _log.info(
        "Scan production_sheets%s — mode %s",
        f" (tenant={args.tenant_id})" if args.tenant_id else "",
        "DRY-RUN" if dry_run else "APPLY",
    )

    stats = Stats()
    rows = run_sql(sql, params) or []

    for row in rows:
        stats.sheets_scanned += 1
        try:
            new_data = _migrate_sheet(
                row, stats=stats, dry_run=dry_run, remove_base64=args.remove_base64,
            )
        except Exception:  # noqa: BLE001
            _log.exception("Sheet %s: erreur fatale, skip", row.get("id"))
            stats.photos_failed += 1
            continue

        if new_data is not None and not dry_run:
            try:
                run_sql(
                    """
                    UPDATE production_sheets
                    SET data = CAST(:d AS jsonb), updated_at = now()
                    WHERE id = :id
                    """,
                    {"id": row["id"], "d": json.dumps(new_data)},
                )
            except Exception:  # noqa: BLE001
                _log.exception("Sheet %s: UPDATE échec", row.get("id"))
                stats.photos_failed += 1

    stats.print_report(dry_run=dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
