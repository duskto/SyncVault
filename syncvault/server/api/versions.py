from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from syncvault.server.api.uploads import Owner
from syncvault.server.models import File, Version
from syncvault.shared.protocol import ProtocolError

router = APIRouter(prefix="/api/v1/versions", tags=["versions"])


@router.get("/{version_id}/content")
def content(version_id: str, request: Request, user: Owner):
    with request.app.state.sessions() as db:
        version = db.get(Version, version_id)
        if version is None or db.get(File, version.file_id).owner_id != user:
            raise ProtocolError(404, "NOT_FOUND", "版本不存在")
        path = request.app.state.storage.path(version.object_key)
        if not path.is_file():
            raise ProtocolError(503, "OBJECT_MISSING", "版本对象暂不可用")
        return FileResponse(
            path, media_type="application/octet-stream",
            headers={"ETag": f'"{version.sha256}"', "X-Content-SHA256": version.sha256},
        )
