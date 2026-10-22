import fcntl
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from syncvault.shared.paths import namespace as validate_namespace


class State:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        self.directory = self.root / ".syncvault"
        if self.directory.is_symlink():
            raise ValueError("状态目录不得是符号链接")
        for name in ("config.json", "state.db", "snapshots", "client.lock"):
            if (self.directory / name).is_symlink():
                raise ValueError(f"状态目录内不得使用符号链接: {name}")
        if not (self.directory / "config.json").is_file():
            raise ValueError(f"目录尚未初始化: {root}；请先运行 syncvault init")
        self.config = json.loads((self.directory / "config.json").read_text())
        self.db = sqlite3.connect(self.directory / "state.db", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS file_manifest (
                logical_path TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL, sha256 TEXT NOT NULL,
                last_synced_version TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_uploads (
                logical_path TEXT PRIMARY KEY, snapshot_path TEXT NOT NULL,
                upload_id TEXT, sha256 TEXT NOT NULL, chunk_size INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS upload_archive (
                id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, payload TEXT NOT NULL
            );
        """)

    @classmethod
    def initialize(cls, root: Path, server: str, namespace: str):
        if not root.is_dir():
            raise ValueError("备份根路径必须是已存在的目录")
        parsed = urlsplit(server)
        if (
            parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise ValueError("server 必须是没有凭据、查询参数或片段的 HTTP(S) URL")
        validate_namespace(namespace)
        directory = root.resolve() / ".syncvault"
        if directory.is_symlink():
            raise ValueError("状态目录不得是符号链接")
        directory.mkdir(mode=0o700, exist_ok=True)
        config_path = directory / "config.json"
        if config_path.is_symlink() or (directory / "snapshots").is_symlink():
            raise ValueError("配置和快照目录不得是符号链接")
        config = {"server": server.rstrip("/"), "namespace": namespace}
        if config_path.exists():
            if json.loads(config_path.read_text()) != config:
                raise ValueError("已有配置不匹配；请使用新的备份目录，避免混用同步基线")
        else:
            with config_path.open("x") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        (directory / "snapshots").mkdir(mode=0o700, exist_ok=True)
        return cls(root)

    @classmethod
    def discover(cls, root: Path | None = None):
        if root is not None:
            return cls(root)
        cwd = Path.cwd()
        for candidate in (cwd, *cwd.parents):
            if (candidate / ".syncvault/config.json").exists():
                return cls(candidate)
        raise ValueError("未找到 .syncvault 配置；请指定 --root")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    @contextmanager
    def lock(self):
        with (self.directory / "client.lock").open("ab") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("此目录已有 push 或任务管理进程正在运行") from error
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def manifest(self):
        return {row["logical_path"]: dict(row) for row in self.db.execute(
            "SELECT * FROM file_manifest"
        )}

    def pending(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM pending_uploads ORDER BY logical_path"
        )]

    def save_pending(self, pending):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO pending_uploads VALUES (?, ?, ?, ?, ?, unixepoch(), ?)",
                (pending["logical_path"], pending["snapshot_path"], pending.get("upload_id"),
                 pending["sha256"], pending["chunk_size"], json.dumps(pending)),
            )

    def archived(self):
        return [{"archive_id": row[0], "created_at": row[1], **json.loads(row[2])}
                for row in self.db.execute(
                    "SELECT id, created_at, payload FROM upload_archive ORDER BY created_at, id"
                )]

    def replace_pending(self, previous, replacement):
        # Keep the previous snapshot referenced even after the replacement commits.
        archive_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO upload_archive VALUES (?, unixepoch(), ?)",
                            (archive_id, json.dumps(previous)))
            self.db.execute(
                "INSERT OR REPLACE INTO pending_uploads VALUES (?, ?, ?, ?, ?, unixepoch(), ?)",
                (replacement["logical_path"], replacement["snapshot_path"],
                 replacement.get("upload_id"), replacement["sha256"], replacement["chunk_size"],
                 json.dumps(replacement)),
            )
        return archive_id

    def committed(self, pending, version):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO file_manifest VALUES (?, ?, ?, ?, ?)",
                (pending["logical_path"], pending["size_bytes"], pending["mtime_ns"],
                 pending["sha256"], version["version_id"]),
            )
            self.db.execute(
                "DELETE FROM pending_uploads WHERE logical_path = ?", (pending["logical_path"],)
            )

    def snapshot_path(self, name: str) -> Path:
        # SQLite is local state, but never allow it to direct cleanup outside snapshots.
        if Path(name).name != name or not name:
            raise ValueError("非法快照路径")
        path = self.directory / "snapshots" / name
        if path.is_symlink():
            raise ValueError("快照不得是符号链接")
        return path

    def snapshot_references(self):
        return {p["snapshot_path"] for p in [*self.pending(), *self.archived()]}

    def remove_snapshot_if_unreferenced(self, name):
        if name not in self.snapshot_references():
            self.snapshot_path(name).unlink(missing_ok=True)

    def cleanup_snapshots(self):
        referenced = self.snapshot_references()
        for path in (self.directory / "snapshots").iterdir():
            if path.name not in referenced and path.is_file() and not path.is_symlink():
                path.unlink()
