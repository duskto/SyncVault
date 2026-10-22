"""Explicit, recoverable operations on pending uploads; never silently change the base."""

import hashlib
import os
import stat
import tempfile
import uuid
from pathlib import Path

from syncvault.client.hashing import hash_file
from syncvault.client.snapshot import snapshot
from syncvault.client.transfer import validate_version
from syncvault.client.transport import APIError
from syncvault.shared.paths import logical_path


def find_pending(state, path):
    path = logical_path(path)
    for item in state.pending():
        if item["logical_path"] == path:
            return item
    raise ValueError(f"没有待处理上传: {path}")


def check_snapshot(state, pending):
    source = state.snapshot_path(pending["snapshot_path"])
    if (not source.is_file() or source.stat().st_size != pending["size_bytes"]
            or hash_file(source) != pending["sha256"]):
        raise ValueError("快照丢失或损坏；如需重新备份当前文件，请显式使用 --from-source")
    return source


def export_snapshot(state, path, output: Path, archive_id=None):
    with state.lock():
        if archive_id:
            pending = next((item for item in state.archived()
                            if item["archive_id"] == archive_id
                            and item["logical_path"] == logical_path(path)), None)
            if pending is None:
                raise ValueError("未找到此文件对应的归档任务")
        else:
            pending = find_pending(state, path)
        source = state.snapshot_path(pending["snapshot_path"])
        output = output.absolute()
        if output.resolve().is_relative_to(state.directory.resolve()):
            raise ValueError("导出目标不得位于 .syncvault 状态目录中")
        if output.exists() or output.is_symlink():
            raise FileExistsError(f"导出目标已存在: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".syncvault-export-", dir=output.parent)
        temporary = Path(name)
        try:
            digest, size = hashlib.sha256(), 0
            with os.fdopen(descriptor, "wb") as target:
                descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise ValueError("快照不是普通文件")
                    while data := handle.read(1024 * 1024):
                        size += len(data)
                        if size > pending["size_bytes"]:
                            raise ValueError("快照校验失败：长度超出预期")
                        target.write(data)
                        digest.update(data)
                target.flush()
                os.fsync(target.fileno())
            if size != pending["size_bytes"] or digest.hexdigest() != pending["sha256"]:
                raise ValueError("快照校验失败，未发布导出文件")
            os.link(temporary, output)
            directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": path, "output": str(output), "sha256": pending["sha256"]}


def settle_remote(state, remote, pending):
    """Cancel OPEN, reconcile COMPLETED, and refuse an active FINALIZING upload."""
    upload_id = pending.get("upload_id")
    if upload_id is None:
        # Do not create a session merely to cancel it; even rejected/oversize tasks
        # must be pausable. The owner-scoped lookup also recovers a lost receipt.
        try:
            info = remote.request("GET", "/api/v1/uploads",
                                  params={"idempotency_key": pending["idempotency_key"]}).json()
        except APIError as error:
            if error.status == 404:
                return None
            raise
        pending["upload_id"] = info["upload_id"]
        state.save_pending(pending)
    else:
        try:
            info = remote.request("GET", f"/api/v1/uploads/{upload_id}").json()
        except APIError as error:
            if error.status in (404, 410):
                return None
            raise
    if info["status"] == "OPEN":
        try:
            remote.request("DELETE", f"/api/v1/uploads/{pending['upload_id']}")
            return None
        except APIError as error:
            if error.status not in (409, 410):
                raise
            # Complete or expiration may have won the race with cancellation.
            info = remote.request("GET", f"/api/v1/uploads/{pending['upload_id']}").json()
    if info["status"] == "COMPLETED":
        validate_version(pending, info["version"])
        state.committed(pending, info["version"])
        state.remove_snapshot_if_unreferenced(pending["snapshot_path"])
        return info["version"]
    if info["status"] not in ("FAILED", "CANCELLED", "EXPIRED"):
        raise ValueError("服务端仍在合并；请等待完成或运行服务端恢复任务后再操作")
    return None


def cancel_pending(state, remote, path):
    with state.lock():
        pending = find_pending(state, path)
        version = settle_remote(state, remote, pending)
        if version:
            return {"path": path, "status": "completed", "version": version}
        pending["paused"] = True
        state.save_pending(pending)
        return {"path": path, "status": "paused",
                "snapshot_retained": state.snapshot_path(pending["snapshot_path"]).is_file()}


def retry_pending(state, remote, path, base_version=None, from_source=False):
    with state.lock():
        pending = find_pending(state, path)
        base = pending["base_version_id"]
        if base_version is not None:
            current = remote.find_file(state.config["namespace"], path)
            if current["current_version_id"] != base_version:
                raise ValueError("指定基准不是远端当前版本；请重新查看 versions 后明确选择")
            base = base_version
        if from_source:
            replacement = snapshot(state, path, pending["chunk_size"], base)
        else:
            check_snapshot(state, pending)
            replacement = {**pending, "base_version_id": base, "upload_id": None,
                           "idempotency_key": uuid.uuid4().hex, "paused": False}
        try:
            version = settle_remote(state, remote, pending)
            if version:
                return {"path": path, "status": "completed", "version": version}
            archive_id = state.replace_pending(pending, replacement)
            return {"path": path, "status": "queued", "archive_id": archive_id,
                    "base_version_id": base, "sha256": replacement["sha256"]}
        finally:
            if from_source:
                state.remove_snapshot_if_unreferenced(replacement["snapshot_path"])
