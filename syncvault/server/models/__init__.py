import time
import uuid

from sqlalchemy import BigInteger, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> int:
    return int(time.time())


class Base(DeclarativeBase):
    pass


class File(Base):
    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("owner_id", "namespace", "path_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(255), index=True)
    namespace: Mapped[str] = mapped_column(String(64))
    logical_path: Mapped[str] = mapped_column(String(2048))
    path_key: Mapped[str] = mapped_column(String(2048))
    current_version_id: Mapped[str | None] = mapped_column(String(32))
    deleted_at: Mapped[int | None] = mapped_column(BigInteger)


class Upload(Base):
    __tablename__ = "upload_sessions"
    __table_args__ = (UniqueConstraint("owner_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(255), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id"))
    base_version_id: Mapped[str | None] = mapped_column(String(32))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    file_sha256: Mapped[str] = mapped_column(String(64))
    chunk_size: Mapped[int] = mapped_column(Integer)
    total_chunks: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now)
    committed_version_id: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(64))


class Chunk(Base):
    __tablename__ = "upload_chunks"
    upload_id: Mapped[str] = mapped_column(ForeignKey("upload_sessions.id"), primary_key=True)
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(255))


class Version(Base):
    __tablename__ = "versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id"), index=True)
    upload_id: Mapped[str] = mapped_column(ForeignKey("upload_sessions.id"), unique=True)
    object_key: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
