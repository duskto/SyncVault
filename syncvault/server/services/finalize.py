import hashlib
import os
import uuid

from sqlalchemy import select, update

from syncvault.client.hashing import hash_file, iter_chunks
from syncvault.server.models import Chunk, File, Upload, Version, now
from syncvault.server.services.uploads import owned_upload, require_open, version_info
from syncvault.shared.protocol import ProtocolError


def publish_metadata(sessions, upload_id: str):
    conflict = False
    with sessions.begin() as db:
        upload = db.get(Upload, upload_id)
        if upload.status == "COMPLETED":
            return version_info(db.get(Version, upload.committed_version_id))
        version = Version(
            id=upload.id, file_id=upload.file_id, upload_id=upload.id,
            object_key=f"objects/{upload.id}", size_bytes=upload.size_bytes,
            sha256=upload.file_sha256,
        )
        # The UPDATE is a database compare-and-swap, across different upload locks.
        changed = db.execute(
            update(File).where(
                File.id == upload.file_id,
                File.current_version_id == upload.base_version_id,
            ).values(current_version_id=version.id)
        ).rowcount
        if not changed:
            upload.status = "FAILED"
            upload.failure_code = "VERSION_CONFLICT"
            conflict = True
        else:
            db.add(version)
            upload.status = "COMPLETED"
            upload.committed_version_id = version.id
            db.flush()
            result = version_info(version)
        upload.updated_at = now()
    if conflict:
        raise ProtocolError(409, "VERSION_CONFLICT", "其他上传已提交新版本，保留待处理对象")
    return result


def complete(sessions, storage, owner: str, upload_id: str):
    with sessions() as db:
        owned_upload(db, upload_id, owner)
    # A process crash releases flock. Recovery can reclaim FINALIZING immediately;
    # there is no lease that could expire while a slow merger is still writing.
    with storage.lock(upload_id, blocking=False):
        with sessions.begin() as db:
            upload = owned_upload(db, upload_id, owner)
            if upload.status == "COMPLETED":
                return version_info(db.get(Version, upload.committed_version_id))
            if upload.status not in ("OPEN", "FINALIZING"):
                raise ProtocolError(
                    409, upload.failure_code or "INVALID_STATE", f"上传状态为 {upload.status}"
                )
            if upload.status == "OPEN":
                require_open(upload)
            chunks = db.scalars(
                select(Chunk).where(Chunk.upload_id == upload_id).order_by(Chunk.chunk_index)
            ).all()
            received = {chunk.chunk_index for chunk in chunks}
            missing = sorted(set(range(upload.total_chunks)) - received)
            if missing:
                raise ProtocolError(409, "MISSING_CHUNKS", "仍有缺失块", missing_chunks=missing)
            upload.status = "FINALIZING"
            upload.updated_at = now()
        key = f"objects/{upload_id}"
        target = storage.path(key)
        temporary = storage.path(f"tmp/{upload_id}-{uuid.uuid4().hex}.merge")
        try:
            if target.exists():
                size, digest = target.stat().st_size, hash_file(target)
            else:
                digest_builder = hashlib.sha256()
                size = 0
                with temporary.open("xb") as output:
                    for chunk in chunks:
                        chunk_digest = hashlib.sha256()
                        chunk_size = 0
                        for data in iter_chunks(storage.path(chunk.storage_key)):
                            output.write(data)
                            digest_builder.update(data)
                            chunk_digest.update(data)
                            size += len(data)
                            chunk_size += len(data)
                        if (
                            chunk_digest.hexdigest() != chunk.sha256
                            or chunk_size != chunk.size_bytes
                        ):
                            raise ProtocolError(422, "INTEGRITY", "服务端块内容已损坏")
                    output.flush()
                    os.fsync(output.fileno())
                digest = digest_builder.hexdigest()
            if size != upload.size_bytes or digest != upload.file_sha256:
                raise ProtocolError(422, "INTEGRITY", "整文件大小或 SHA-256 校验失败")
            if not target.exists():
                storage.publish(temporary, key)
            return publish_metadata(sessions, upload_id)
        except ProtocolError as error:
            if error.code == "INTEGRITY":
                with sessions.begin() as db:
                    failed = db.get(Upload, upload_id)
                    failed.status = "FAILED"
                    failed.failure_code = "INTEGRITY"
                    failed.updated_at = now()
            raise
        finally:
            temporary.unlink(missing_ok=True)
