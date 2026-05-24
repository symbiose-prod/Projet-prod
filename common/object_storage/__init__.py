"""
common/object_storage/
======================
Stockage objet pour les binaires lourds (photos, PDF) hors PostgreSQL.

Aujourd'hui les photos d'incidents des fiches production sont stockées
en base64 dans le JSONB de ``production_sheets.data``. Ce module permet
de les externaliser vers OVH Object Storage (compatible S3).

⚠️ Renommé depuis ``common/storage/`` pour éviter le conflit avec le
module ``common/storage.py`` (snapshots production proposals).

Cf. docs/architecture-audit.md §7 (Sprint Photos S3).
"""
from common.object_storage.ovh_s3 import (
    OVHStorageError,
    delete_object,
    delete_photo,
    generate_photo_key,
    get_presigned_url,
    is_configured,
    list_objects,
    upload_file,
    upload_photo,
)

__all__ = [
    "OVHStorageError",
    "delete_object",
    "delete_photo",
    "generate_photo_key",
    "get_presigned_url",
    "is_configured",
    "list_objects",
    "upload_file",
    "upload_photo",
]
