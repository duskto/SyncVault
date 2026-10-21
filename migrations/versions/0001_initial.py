"""Initial immutable-version upload schema. Frozen independently of ORM models."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "files",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("logical_path", sa.String(2048), nullable=False),
        sa.Column("path_key", sa.String(2048), nullable=False),
        sa.Column("current_version_id", sa.String(32)),
        sa.Column("deleted_at", sa.BigInteger),
        sa.UniqueConstraint("owner_id", "namespace", "path_key"),
    )
    op.create_index("ix_files_owner_id", "files", ["owner_id"])
    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("file_id", sa.String(32), sa.ForeignKey("files.id"), nullable=False),
        sa.Column("base_version_id", sa.String(32)),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("chunk_size", sa.Integer, nullable=False),
        sa.Column("total_chunks", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.BigInteger, nullable=False),
        sa.Column("updated_at", sa.BigInteger, nullable=False),
        sa.Column("committed_version_id", sa.String(32)),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("failure_code", sa.String(64)),
        sa.UniqueConstraint("owner_id", "idempotency_key"),
    )
    op.create_index("ix_upload_sessions_owner_id", "upload_sessions", ["owner_id"])
    op.create_index("ix_upload_sessions_status", "upload_sessions", ["status"])
    op.create_table(
        "upload_chunks",
        sa.Column(
            "upload_id", sa.String(32), sa.ForeignKey("upload_sessions.id"), primary_key=True
        ),
        sa.Column("chunk_index", sa.Integer, primary_key=True),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False),
    )
    op.create_table(
        "versions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("file_id", sa.String(32), sa.ForeignKey("files.id"), nullable=False),
        sa.Column("upload_id", sa.String(32), sa.ForeignKey("upload_sessions.id"), nullable=False),
        sa.Column("object_key", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False),
        sa.UniqueConstraint("upload_id"),
    )
    op.create_index("ix_versions_file_id", "versions", ["file_id"])


def downgrade():
    for table in ("versions", "upload_chunks", "upload_sessions", "files"):
        op.drop_table(table)
