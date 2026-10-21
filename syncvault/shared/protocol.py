from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from syncvault.shared.paths import logical_path, namespace

SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class UploadRequest(BaseModel):
    namespace: str
    logical_path: str
    size_bytes: int = Field(ge=0)
    sha256: SHA256
    chunk_size: int = Field(ge=1, le=64 * 1024 * 1024)
    base_version_id: str | None = None

    _path = field_validator("logical_path")(logical_path)
    _namespace = field_validator("namespace")(namespace)


class ProtocolError(Exception):
    def __init__(self, status: int, code: str, message: str, **details):
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)
