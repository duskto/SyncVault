from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from syncvault.server.api.uploads import Owner
from syncvault.server.models import File, Version
from syncvault.server.services.uploads import version_info
from syncvault.shared.protocol import ProtocolError

router = APIRouter(prefix="/api/v1/files", tags=["files"])


@router.get("")
def files(
    request: Request, user: Owner, namespace: str,
    logical_path: str | None = None, offset: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=1000),
):
    with request.app.state.sessions() as db:
        query = select(File).where(
            File.owner_id == user, File.namespace == namespace, File.current_version_id.is_not(None)
        )
        if logical_path is not None:
            query = query.where(File.logical_path == logical_path)
        result = db.scalars(query.order_by(File.logical_path).offset(offset).limit(limit)).all()
        return [{
            "file_id": file.id, "namespace": file.namespace, "logical_path": file.logical_path,
            "current_version_id": file.current_version_id,
            "current_version": version_info(db.get(Version, file.current_version_id)),
        } for file in result]


@router.get("/{file_id}/versions")
def versions(
    file_id: str, request: Request, user: Owner,
    offset: int = Query(0, ge=0), limit: int = Query(1000, ge=1, le=1000),
):
    with request.app.state.sessions() as db:
        file = db.get(File, file_id)
        if file is None or file.owner_id != user:
            raise ProtocolError(404, "NOT_FOUND", "文件不存在")
        result = db.scalars(
            select(Version).where(Version.file_id == file_id)
            .order_by(Version.created_at.desc(), Version.id).offset(offset).limit(limit)
        ).all()
        return [version_info(version) for version in result]
