import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from syncvault.server.models import Chunk, File, Upload, Version, now
from syncvault.shared.paths import path_key
from syncvault.shared.protocol import ProtocolError, UploadRequest


def owned_upload(db, upload_id: str, owner: str) -> Upload:
    upload = db.get(Upload, upload_id)
    if upload is None or upload.owner_id != owner:
        raise ProtocolError(404, "NOT_FOUND", "上传会话不存在")
    return upload


def require_open(upload: Upload):
    if upload.status == "OPEN" and upload.expires_at <= now():
        raise ProtocolError(410, "EXPIRED", "上传会话已过期")
    if upload.status != "OPEN":
        raise ProtocolError(409, "INVALID_STATE", f"上传状态为 {upload.status}")


def version_info(version: Version) -> dict:
    return {
        "version_id": version.id,
        "file_id": version.file_id,
        "upload_id": version.upload_id,
        "size_bytes": version.size_bytes,
        "sha256": version.sha256,
        "created_at": version.created_at,
    }


def upload_info(db, upload: Upload) -> dict:
    chunks = db.scalars(
        select(Chunk).where(Chunk.upload_id == upload.id).order_by(Chunk.chunk_index)
    ).all()
    result = {
        "upload_id": upload.id,
        "file_id": upload.file_id,
        "status": (
            "EXPIRED" if upload.status == "OPEN" and upload.expires_at <= now() else upload.status
        ),
        "size_bytes": upload.size_bytes,
        "sha256": upload.file_sha256,
        "chunk_size": upload.chunk_size,
        "total_chunks": upload.total_chunks,
        "received_chunks": [c.chunk_index for c in chunks],
        "expires_at": upload.expires_at,
        "failure_code": upload.failure_code,
        "committed_version_id": upload.committed_version_id,
    }
    if upload.committed_version_id:
        result["version"] = version_info(db.get(Version, upload.committed_version_id))
    return result


def initialize(sessions, settings, owner: str, body: UploadRequest, key: str | None) -> dict:
    if body.size_bytes > settings.max_file_size:
        raise ProtocolError(413, "FILE_TOO_LARGE", "文件超过服务端大小限制")
    count = (body.size_bytes + body.chunk_size - 1) // body.chunk_size
    if count > settings.max_chunks:
        raise ProtocolError(422, "TOO_MANY_CHUNKS", "分块数过多，请增大 chunk_size")
    if key is not None and (not key or len(key) > 128):
        raise ProtocolError(422, "INVALID_KEY", "Idempotency-Key 长度必须为 1–128")
    fingerprint = hashlib.sha256(
        json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    # Unique constraints arbitrate races creating the file or the idempotency key.
    for attempt in range(3):
        try:
            with sessions.begin() as db:
                if key:
                    existing = db.scalar(
                        select(Upload).where(
                            Upload.owner_id == owner, Upload.idempotency_key == key
                        )
                    )
                    if existing:
                        if existing.request_hash != fingerprint:
                            raise ProtocolError(409, "IDEMPOTENCY_CONFLICT", "幂等键参数不一致")
                        return upload_info(db, existing)
                file = db.scalar(
                    select(File).where(
                        File.owner_id == owner,
                        File.namespace == body.namespace,
                        File.path_key == path_key(body.logical_path),
                    )
                )
                if file and file.logical_path != body.logical_path:
                    raise ProtocolError(409, "PATH_COLLISION", "路径大小写冲突")
                if file is None:
                    # The API serializes creation within one namespace. Checking
                    # components also catches A/x versus a/y and file/directory clashes.
                    paths = db.scalars(select(File.logical_path).where(
                        File.owner_id == owner, File.namespace == body.namespace
                    ))
                    requested = body.logical_path.split("/")
                    for existing_path in paths:
                        existing_parts = existing_path.split("/")
                        for left, right in zip(existing_parts, requested):
                            if path_key(left) != path_key(right):
                                break
                            if left != right:
                                raise ProtocolError(409, "PATH_COLLISION", "目录路径大小写冲突")
                        else:
                            if len(existing_parts) != len(requested):
                                raise ProtocolError(409, "PATH_COLLISION", "文件与目录路径冲突")
                    file = File(
                        owner_id=owner,
                        namespace=body.namespace,
                        logical_path=body.logical_path,
                        path_key=path_key(body.logical_path),
                    )
                    db.add(file)
                    db.flush()
                if file.current_version_id != body.base_version_id:
                    raise ProtocolError(
                        409, "VERSION_CONFLICT", "基准版本已变化",
                        current_version_id=file.current_version_id,
                    )
                upload = Upload(
                    owner_id=owner, file_id=file.id,
                    base_version_id=body.base_version_id, size_bytes=body.size_bytes,
                    file_sha256=body.sha256, chunk_size=body.chunk_size,
                    total_chunks=count, expires_at=now() + settings.session_ttl,
                    idempotency_key=key, request_hash=fingerprint,
                )
                db.add(upload)
                db.flush()
                return upload_info(db, upload)
        except IntegrityError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")
