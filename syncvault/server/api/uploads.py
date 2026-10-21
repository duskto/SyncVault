import hashlib
import json
import os
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from starlette.concurrency import run_in_threadpool

from syncvault.server.api.dependencies import owner
from syncvault.server.models import now
from syncvault.server.services.chunks import expected_size, publish_chunk
from syncvault.server.services.finalize import complete
from syncvault.server.services.uploads import (
    initialize,
    owned_upload,
    require_open,
    upload_info,
)
from syncvault.shared.protocol import ProtocolError, UploadRequest

router = APIRouter(prefix="/api/v1/uploads", tags=["uploads"])
Owner = Annotated[str, Depends(owner)]


@router.post("")
def create_upload(
    body: UploadRequest, request: Request, user: Owner,
    idempotency_key: str | None = Header(default=None),
):
    state = request.app.state
    scope = hashlib.sha256(json.dumps([user, body.namespace]).encode()).hexdigest()
    with state.storage.lock(f"namespace-{scope}"):
        return initialize(state.sessions, state.settings, user, body, idempotency_key)


@router.get("/{upload_id}")
def get_upload(upload_id: str, request: Request, user: Owner):
    with request.app.state.sessions() as db:
        return upload_info(db, owned_upload(db, upload_id, user))


@router.put("/{upload_id}/chunks/{index}")
async def put_chunk(
    upload_id: str, index: int, request: Request, user: Owner,
    x_chunk_sha256: str | None = Header(default=None),
):
    state = request.app.state

    def inspect():
        with state.sessions() as db:
            upload = owned_upload(db, upload_id, user)
            require_open(upload)
            return expected_size(upload, index)

    expected = await run_in_threadpool(inspect)
    if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
        raise ProtocolError(415, "CONTENT_TYPE", "块必须为 application/octet-stream")
    temporary = state.storage.path(f"tmp/{upload_id}-{uuid.uuid4().hex}.chunk")
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as output:
            async for data in request.stream():
                size += len(data)
                if size > expected:
                    raise ProtocolError(422, "CHUNK_SIZE", "块长度超过预期")
                digest.update(data)
                await run_in_threadpool(output.write, data)
            await run_in_threadpool(output.flush)
            await run_in_threadpool(os.fsync, output.fileno())
        actual_hash = digest.hexdigest()
        if size != expected:
            raise ProtocolError(422, "CHUNK_SIZE", "块长度错误")
        if x_chunk_sha256 and x_chunk_sha256 != actual_hash:
            raise ProtocolError(422, "CHUNK_HASH", "块 SHA-256 不匹配")
        return await run_in_threadpool(
            publish_chunk, state.sessions, state.storage, user, upload_id, index,
            temporary, size, actual_hash,
        )
    finally:
        temporary.unlink(missing_ok=True)


@router.post("/{upload_id}/complete")
def finish(upload_id: str, request: Request, user: Owner):
    state = request.app.state
    return complete(state.sessions, state.storage, user, upload_id)


@router.delete("/{upload_id}")
def cancel(upload_id: str, request: Request, user: Owner):
    state = request.app.state
    with state.sessions() as db:
        owned_upload(db, upload_id, user)
    with state.storage.lock(upload_id), state.sessions.begin() as db:
        upload = owned_upload(db, upload_id, user)
        if upload.status == "CANCELLED":
            return {"status": "CANCELLED"}
        require_open(upload)
        upload.status = "CANCELLED"
        upload.updated_at = now()
    return {"status": "CANCELLED"}
