import hashlib
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import httpx

from syncvault.client.hashing import hash_file
from syncvault.client.planner import plan
from syncvault.client.snapshot import snapshot
from syncvault.client.transport import APIError


class PushFailed(RuntimeError):
    def __init__(self, versions, failures):
        self.versions = versions
        self.failures = failures
        super().__init__(f"{len(failures)} 个文件上传失败；其他文件已处理，详见失败列表")


def validate_version(pending, version):
    if (
        version.get("sha256") != pending["sha256"]
        or version.get("size_bytes") != pending["size_bytes"]
        or not version.get("version_id")
    ):
        raise ValueError("服务端完成回执与本地快照不一致，不更新清单")


def upload_pending(state, remote, pending, concurrency=4, progress=lambda _: None):
    info = None
    if pending["upload_id"]:
        try:
            info = remote.request("GET", f"/api/v1/uploads/{pending['upload_id']}").json()
        except APIError as error:
            if error.status not in (404, 410):
                raise
        if info and info["status"] == "COMPLETED":
            validate_version(pending, info["version"])
            state.committed(pending, info["version"])
            state.remove_snapshot_if_unreferenced(pending["snapshot_path"])
            return info["version"]
        if info and info["status"] in ("FAILED", "CANCELLED"):
            raise ValueError(f"会话 {info['status']}: {info.get('failure_code')}；快照已保留")
        if info is None or info["status"] == "EXPIRED":
            pending["upload_id"] = None
            pending["idempotency_key"] = uuid.uuid4().hex
            state.save_pending(pending)
            info = None
    source = state.snapshot_path(pending["snapshot_path"])
    if not source.is_file() or hash_file(source) != pending["sha256"]:
        raise ValueError("断点快照丢失或损坏，拒绝使用变化后的源文件续传；pending 已保留")
    if info is None:
        body = {name: pending[name] for name in (
            "logical_path", "size_bytes", "sha256", "chunk_size", "base_version_id"
        )}
        body["namespace"] = state.config["namespace"]
        info = remote.request(
            "POST", "/api/v1/uploads", json=body,
            headers={"Idempotency-Key": pending["idempotency_key"]},
        ).json()
        pending["upload_id"] = info["upload_id"]
        state.save_pending(pending)
        # An initialization response may have been lost on an earlier attempt.
        if info["status"] == "COMPLETED":
            validate_version(pending, info["version"])
            state.committed(pending, info["version"])
            state.remove_snapshot_if_unreferenced(pending["snapshot_path"])
            return info["version"]
        if info["status"] == "EXPIRED":
            return upload_pending(state, remote, pending, concurrency, progress)
    if info["sha256"] != pending["sha256"] or info["chunk_size"] != pending["chunk_size"]:
        raise ValueError("远端会话参数与快照不一致")
    upload_id = pending["upload_id"]
    received = set(info["received_chunks"])
    missing = iter(index for index in range(info["total_chunks"]) if index not in received)
    progress({"path": pending["logical_path"], "resumed_chunks": len(received),
              "total_chunks": info["total_chunks"]})

    def send(index):
        with source.open("rb") as handle:
            handle.seek(index * pending["chunk_size"])
            data = handle.read(pending["chunk_size"])
        remote.request(
            "PUT", f"/api/v1/uploads/{upload_id}/chunks/{index}", content=data,
            headers={"Content-Type": "application/octet-stream",
                     "X-Chunk-SHA256": hashlib.sha256(data).hexdigest()},
        )
        return len(data)

    if info["status"] == "OPEN":
        # One global pool, sequential files and a bounded queue: at most P live chunks.
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = set()
            for _ in range(concurrency):
                index = next(missing, None)
                if index is not None:
                    futures.add(pool.submit(send, index))
            while futures:
                completed, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    size = future.result()
                    progress({"path": pending["logical_path"], "confirmed_bytes": size})
                    index = next(missing, None)
                    if index is not None:
                        futures.add(pool.submit(send, index))
    version = remote.request("POST", f"/api/v1/uploads/{upload_id}/complete").json()
    validate_version(pending, version)
    state.committed(pending, version)
    state.remove_snapshot_if_unreferenced(pending["snapshot_path"])
    return version


def push(state, remote, concurrency=4, chunk_size=4 * 1024 * 1024, full=False,
         progress=lambda _: None, continue_on_error=False):
    if not 1 <= concurrency <= 32 or not 1 <= chunk_size <= 64 * 1024 * 1024:
        raise ValueError("concurrency 必须为 1–32，chunk_size 必须为 1–64MiB 字节")
    versions = []
    failures = []

    def attempt(path, operation):
        try:
            versions.append(operation())
        except (ValueError, OSError, APIError, httpx.TransportError) as error:
            if not continue_on_error:
                raise
            failure = {"path": path, "error": str(error)}
            failures.append(failure)
            progress(failure)

    with state.lock():
        state.cleanup_snapshots()
        for pending in state.pending():
            if pending.get("paused"):
                progress({"path": pending["logical_path"], "action": "skip", "reason": "任务已暂停"})
                continue
            attempt(pending["logical_path"],
                    lambda: upload_pending(state, remote, pending, concurrency, progress))
        manifest = state.manifest()
        for operation in plan(state, full=full):
            if operation.action == "skip":
                progress(operation.as_dict())
                continue
            old = manifest.get(operation.logical_path, {})
            def upload_new():
                pending = snapshot(
                    state, operation.logical_path, chunk_size, old.get("last_synced_version")
                )
                state.save_pending(pending)
                return upload_pending(state, remote, pending, concurrency, progress)

            attempt(operation.logical_path, upload_new)
    if failures:
        raise PushFailed(versions, failures)
    return versions
