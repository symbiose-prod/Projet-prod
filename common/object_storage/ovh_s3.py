"""
common/object_storage/ovh_s3.py
========================
Client S3-compatible pour OVH Object Storage.

OVH expose un service Object Storage compatible avec l'API S3 (AWS S3).
On utilise boto3 avec un endpoint URL personnalisé pour s'y connecter.

Configuration (env vars) :
- ``OVH_S3_ENDPOINT`` (ex: ``https://s3.gra.io.cloud.ovh.net``)
- ``OVH_S3_REGION`` (ex: ``gra``)
- ``OVH_S3_BUCKET`` (nom du bucket dédié aux photos production)
- ``OVH_S3_ACCESS_KEY``
- ``OVH_S3_SECRET_KEY``
- ``OVH_S3_PHOTOS_PREFIX`` (optionnel, défaut ``production/photos/``)

Si les vars ne sont pas définies, ``is_configured()`` retourne False et
les fonctions d'upload lèvent ``OVHStorageError``. Le caller doit fallback
sur le stockage base64 historique.

API :
- ``upload_photo(image_bytes, tenant_id, sheet_id, content_type) -> str``
  Upload une photo, retourne la clé S3 (utilisable avec get_presigned_url).
- ``get_presigned_url(key, ttl_seconds=3600) -> str``
  Génère une URL signée temporaire pour télécharger l'image.
- ``delete_photo(key) -> bool`` (utile pour nettoyage)
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import UTC, datetime

_log = logging.getLogger("ferment.storage.ovh_s3")


# ─── Erreur dédiée ────────────────────────────────────────────────────────


class OVHStorageError(Exception):
    """Erreur de stockage OVH (non configuré, upload échoué, etc.)."""


# ─── Config ──────────────────────────────────────────────────────────────


def _env(key: str) -> str:
    return (os.environ.get(key) or "").strip()


def is_configured() -> bool:
    """True si les vars d'env minimales pour OVH S3 sont présentes."""
    return all([
        _env("OVH_S3_ENDPOINT"),
        _env("OVH_S3_BUCKET"),
        _env("OVH_S3_ACCESS_KEY"),
        _env("OVH_S3_SECRET_KEY"),
    ])


# ─── Client singleton (boto3) ────────────────────────────────────────────


_client_lock = threading.Lock()
_client: object | None = None


def _import_boto3():  # type: ignore[no-untyped-def]
    """Import paresseux de boto3 + botocore.config. Patchable depuis les tests.

    Lève OVHStorageError si boto3 n'est pas installé.
    """
    try:
        import boto3
        from botocore.config import Config
        return boto3, Config
    except ImportError as exc:  # pragma: no cover
        raise OVHStorageError(
            "boto3 non installé : ajouter à requirements.txt"
        ) from exc


def _get_client():  # type: ignore[no-untyped-def]
    """Retourne un client boto3 S3 configuré pour OVH (lazy + thread-safe)."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        if not is_configured():
            raise OVHStorageError(
                "OVH S3 non configuré : définir OVH_S3_ENDPOINT, OVH_S3_BUCKET, "
                "OVH_S3_ACCESS_KEY, OVH_S3_SECRET_KEY dans l'env"
            )
        boto3, Config = _import_boto3()
        _client = boto3.client(
            "s3",
            endpoint_url=_env("OVH_S3_ENDPOINT"),
            aws_access_key_id=_env("OVH_S3_ACCESS_KEY"),
            aws_secret_access_key=_env("OVH_S3_SECRET_KEY"),
            region_name=_env("OVH_S3_REGION") or "gra",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=10,
                read_timeout=30,
            ),
        )
        return _client


def _bucket() -> str:
    return _env("OVH_S3_BUCKET")


def _photos_prefix() -> str:
    """Préfixe pour toutes les photos de production (slash final)."""
    raw = _env("OVH_S3_PHOTOS_PREFIX") or "production/photos/"
    if not raw.endswith("/"):
        raw += "/"
    return raw


# ─── Génération de clés ──────────────────────────────────────────────────


def generate_photo_key(
    *,
    tenant_id: str,
    sheet_id: str,
    extension: str = "jpg",
) -> str:
    """Génère une clé S3 unique pour une photo.

    Format : ``{prefix}{tenant_id}/{YYYY-MM-DD}/{sheet_id}/{uuid}.{ext}``

    Avantages :
    - Partition par tenant (= isolation des objets dans le bucket)
    - Partition par jour (faciles listings/cleanup)
    - Référence du sheet_id pour pouvoir retrouver les photos d'une fiche
    - UUID pour éviter les collisions
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    ext = extension.lower().lstrip(".") or "jpg"
    uid = uuid.uuid4().hex[:16]
    return f"{_photos_prefix()}{tenant_id}/{today}/{sheet_id}/{uid}.{ext}"


# ─── Upload ──────────────────────────────────────────────────────────────


def _detect_extension(content_type: str) -> str:
    """Mappe content-type → extension fichier."""
    ct = (content_type or "").lower().split(";")[0].strip()
    mapping = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/heic": "heic",
    }
    return mapping.get(ct, "jpg")


def upload_photo(
    image_bytes: bytes,
    *,
    tenant_id: str,
    sheet_id: str,
    content_type: str = "image/jpeg",
) -> str:
    """Upload des bytes d'image dans OVH S3. Retourne la clé S3.

    L'objet est créé en ``private`` (pas d'ACL publique). L'accès se fait
    ensuite via ``get_presigned_url()`` qui génère une URL signée TTL.

    Levée d'exception :
    - ``OVHStorageError`` si pas configuré, ou si upload échoue
    """
    if not image_bytes:
        raise OVHStorageError("upload_photo: bytes vides")
    if not (tenant_id and sheet_id):
        raise OVHStorageError("upload_photo: tenant_id et sheet_id requis")

    ext = _detect_extension(content_type)
    key = generate_photo_key(
        tenant_id=tenant_id, sheet_id=sheet_id, extension=ext,
    )
    client = _get_client()

    try:
        client.put_object(  # type: ignore[attr-defined]
            Bucket=_bucket(),
            Key=key,
            Body=image_bytes,
            ContentType=content_type,
            # Metadata pour audit (taille, tenant, sheet)
            Metadata={
                "tenant-id": tenant_id,
                "sheet-id": sheet_id,
                "size-bytes": str(len(image_bytes)),
                "uploaded-at": datetime.now(UTC).isoformat(),
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise OVHStorageError(f"upload_photo failed: {type(exc).__name__}: {exc}") from exc

    _log.info(
        "OVH S3 upload OK : key=%s size=%dKo tenant=%s sheet=%s",
        key, len(image_bytes) // 1024, tenant_id, sheet_id,
    )
    return key


# ─── URL signée (download) ───────────────────────────────────────────────


def get_presigned_url(key: str, *, ttl_seconds: int = 3600) -> str:
    """Génère une URL signée temporaire pour télécharger un objet.

    TTL par défaut 1h. Pour des URLs visibles dans une UI, prévoir un TTL
    suffisant pour la session utilisateur (mais pas trop long pour limiter
    l'exposition si l'URL fuite).
    """
    if not key:
        raise OVHStorageError("get_presigned_url: key vide")
    if ttl_seconds <= 0 or ttl_seconds > 86400 * 7:  # max 7 jours
        raise OVHStorageError(
            f"get_presigned_url: ttl_seconds={ttl_seconds} hors plage [1, 604800]"
        )

    client = _get_client()
    try:
        url = client.generate_presigned_url(  # type: ignore[attr-defined]
            "get_object",
            Params={"Bucket": _bucket(), "Key": key},
            ExpiresIn=ttl_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        raise OVHStorageError(
            f"get_presigned_url failed: {type(exc).__name__}: {exc}"
        ) from exc
    return url


# ─── Suppression (cleanup) ───────────────────────────────────────────────


def delete_photo(key: str) -> bool:
    """Supprime un objet du bucket. Retourne True si succès, False sinon.

    Utilisé pour :
    - Nettoyage manuel via admin
    - Test E2E (cleanup après upload)
    - Suppression d'une fiche production (cleanup associé)

    Idempotent : retourne True même si l'objet n'existe pas (S3 ne lève
    pas d'erreur pour delete d'inexistant).
    """
    if not key:
        return False
    try:
        client = _get_client()
        client.delete_object(Bucket=_bucket(), Key=key)  # type: ignore[attr-defined]
        return True
    except Exception:  # noqa: BLE001
        _log.exception("OVH S3 delete failed: key=%s", key)
        return False


# ─── Upload générique (fichiers arbitraires : backups DB, exports, etc.) ──


def upload_file(
    src_path: str,
    *,
    key: str,
    content_type: str = "application/octet-stream",
    metadata: dict[str, str] | None = None,
) -> str:
    """Upload un fichier local vers le bucket sous une clé arbitraire.

    Contrairement à ``upload_photo`` (qui génère sa clé et est spécifique
    aux photos d'incidents), cette fonction est générique : le caller
    fournit la clé S3 exacte. Utilisée pour les backups DB, exports, etc.

    Args:
        src_path: chemin du fichier local à uploader.
        key: clé S3 (ex. ``"backups/postgres/2026-05-24_ferment.sql.gz"``).
        content_type: ``Content-Type`` HTTP (par défaut octet-stream).
        metadata: méta arbitraires (limité à des string ASCII, S3 le requiert).

    Returns:
        La clé S3 utilisée (= argument ``key``, pour symétrie avec
        ``upload_photo``).

    Raises:
        OVHStorageError: pas configuré, fichier introuvable, ou échec S3.
    """
    import os as _os
    if not src_path or not _os.path.isfile(src_path):
        raise OVHStorageError(f"upload_file: source introuvable: {src_path}")
    if not key:
        raise OVHStorageError("upload_file: key vide")

    client = _get_client()
    size = _os.path.getsize(src_path)
    full_metadata = {
        "size-bytes": str(size),
        "uploaded-at": datetime.now(UTC).isoformat(),
        **(metadata or {}),
    }

    try:
        with open(src_path, "rb") as f:
            client.put_object(  # type: ignore[attr-defined]
                Bucket=_bucket(),
                Key=key,
                Body=f,
                ContentType=content_type,
                Metadata=full_metadata,
            )
    except Exception as exc:  # noqa: BLE001
        raise OVHStorageError(
            f"upload_file failed: {type(exc).__name__}: {exc}",
        ) from exc

    _log.info(
        "OVH S3 upload OK : key=%s size=%dKo (from %s)",
        key, size // 1024, src_path,
    )
    return key


def list_objects(prefix: str) -> list[dict[str, object]]:
    """Liste les objets d'un préfixe donné. Retourne ``[{Key, Size, LastModified}]``.

    Utilisé pour la rotation distante (cleanup des vieux backups S3).
    Pagination automatique (gère >1000 objets).
    """
    if not prefix:
        raise OVHStorageError("list_objects: prefix vide")
    client = _get_client()
    result: list[dict[str, object]] = []
    continuation_token = None
    while True:
        kwargs: dict[str, object] = {"Bucket": _bucket(), "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        try:
            resp = client.list_objects_v2(**kwargs)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            raise OVHStorageError(
                f"list_objects failed: {type(exc).__name__}: {exc}",
            ) from exc
        for obj in resp.get("Contents", []) or []:
            result.append({
                "Key": obj.get("Key", ""),
                "Size": int(obj.get("Size", 0) or 0),
                "LastModified": obj.get("LastModified"),
            })
        if not resp.get("IsTruncated"):
            break
        continuation_token = resp.get("NextContinuationToken")
    return result


def delete_object(key: str) -> bool:
    """Supprime un objet arbitraire (générique, vs ``delete_photo`` legacy).

    Idempotent : retourne True même si l'objet n'existe pas.
    """
    if not key:
        return False
    try:
        client = _get_client()
        client.delete_object(Bucket=_bucket(), Key=key)  # type: ignore[attr-defined]
        return True
    except Exception:  # noqa: BLE001
        _log.exception("OVH S3 delete failed: key=%s", key)
        return False
