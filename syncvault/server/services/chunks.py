from pathlib import Path

from syncvault.server.models import Chunk, now
from syncvault.server.services.uploads import owned_upload, require_open
from syncvault.shared.protocol import ProtocolError


def expected_size(upload, index: int) -> int:
    if index < 0 or index >= upload.total_chunks:
        raise ProtocolError(422, "INVALID_INDEX", "块索引越界")
    return min(upload.chunk_size, upload.size_bytes - index * upload.chunk_size)


def publish_chunk(sessions, storage, owner, upload_id, index, temporary: Path, size, digest):
    # Serialize only publication, not network reception, and coordinate with complete.
    with storage.lock(upload_id), sessions.begin() as db:
        upload = owned_upload(db, upload_id, owner)
        require_open(upload)
        if expected_size(upload, index) != size:
            raise ProtocolError(422, "CHUNK_SIZE", "块长度错误")
        existing = db.get(Chunk, (upload_id, index))
        if existing:
            if existing.sha256 != digest or existing.size_bytes != size:
                raise ProtocolError(409, "CHUNK_CONFLICT", "同一索引已有不同内容")
            return {"chunk_index": index, "sha256": digest, "size_bytes": size}
        key = f"chunks/{upload_id}-{index}-{temporary.name}"
        storage.publish(temporary, key)
        db.add(Chunk(
            upload_id=upload_id, chunk_index=index, size_bytes=size, sha256=digest,
            storage_key=key,
        ))
        upload.updated_at = now()
    return {"chunk_index": index, "sha256": digest, "size_bytes": size}
