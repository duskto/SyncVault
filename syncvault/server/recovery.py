"""Explicit recovery and conservative garbage collection for the local POSIX store."""

import argparse
import json
import time

from sqlalchemy import delete, select

from syncvault.server.config import Settings
from syncvault.server.database import connect_database
from syncvault.server.models import Chunk, Upload
from syncvault.server.services.finalize import complete
from syncvault.server.storage import Storage
from syncvault.shared.protocol import ProtocolError


def recover(sessions, storage):
    outcomes = []
    with sessions() as db:
        interrupted = db.scalars(select(Upload).where(Upload.status == "FINALIZING")).all()
    for upload in interrupted:
        try:
            version = complete(sessions, storage, upload.owner_id, upload.id)
            outcomes.append({"upload_id": upload.id, "version": version})
        except (ProtocolError, OSError) as error:
            outcomes.append({"upload_id": upload.id, "error": str(error)})
    return outcomes


def cleanup(sessions, storage, grace_seconds=3600):
    cutoff = int(time.time()) - grace_seconds
    removed = 0
    with sessions() as db:
        ids = db.scalars(select(Upload.id)).all()
    for upload_id in ids:
        try:
            with storage.lock(upload_id, blocking=False), sessions.begin() as db:
                upload = db.get(Upload, upload_id)
                if upload.status == "OPEN" and upload.expires_at <= int(time.time()):
                    upload.status = "EXPIRED"
                if upload.status in ("COMPLETED", "EXPIRED", "CANCELLED"):
                    chunks = db.scalars(select(Chunk).where(Chunk.upload_id == upload_id)).all()
                    if upload.updated_at < cutoff:
                        # After a crash leftover paths are picked up by the orphan pass below.
                        db.execute(delete(Chunk).where(Chunk.upload_id == upload_id))
                        for chunk in chunks:
                            storage.path(chunk.storage_key).unlink(missing_ok=True)
                            removed += 1
        except ProtocolError:
            continue
    # Lock the owning upload while rechecking references; never remove active request
    # temp files or FAILED objects (conflicting content is retained for manual handling).
    for category in ("tmp", "chunks", "objects"):
        for path in storage.path(category).iterdir():
            if not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
            upload_id = path.name.split("-")[0]
            try:
                with storage.lock(upload_id, blocking=False), sessions() as db:
                    upload = db.get(Upload, upload_id)
                    discard = upload is None
                    if category == "chunks":
                        discard = db.scalar(select(Chunk).where(
                            Chunk.storage_key == f"chunks/{path.name}"
                        )) is None
                    elif category == "tmp":
                        discard = upload is None or upload.status in (
                            "COMPLETED", "EXPIRED", "CANCELLED", "FAILED"
                        )
                    elif category == "objects":
                        discard = upload is None or upload.status in ("EXPIRED", "CANCELLED")
                    if discard:
                        path.unlink(missing_ok=True)
                        removed += 1
            except ProtocolError:
                continue
    return {"removed_artifacts": removed}


def main():
    parser = argparse.ArgumentParser(description="恢复 FINALIZING 会话并清理无引用临时数据")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--grace-seconds", type=int, default=3600)
    args = parser.parse_args()
    if args.grace_seconds < 60:
        parser.error("grace-seconds 必须至少为 60")
    settings = Settings.from_env()
    engine, sessions = connect_database(settings.database_url)
    storage = Storage(settings.storage_root)
    try:
        result = {"recovered": recover(sessions, storage)}
        if args.cleanup:
            result["cleanup"] = cleanup(sessions, storage, args.grace_seconds)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
