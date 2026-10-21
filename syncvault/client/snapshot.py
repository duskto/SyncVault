import hashlib
import os
import stat
import uuid


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def snapshot(state, relative: str, chunk_size: int, base_version_id: str | None):
    source = state.root / relative
    if source.is_symlink() or not source.resolve().is_relative_to(state.root):
        raise ValueError(f"源文件已变成符号链接或越界: {relative}")
    name = uuid.uuid4().hex
    destination = state.snapshot_path(name)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with os.fdopen(descriptor, "rb") as input_file:
            before = os.fstat(input_file.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"不是普通文件: {relative}")
            digest = hashlib.sha256()
            with destination.open("xb") as output:
                while data := input_file.read(4 * 1024 * 1024):
                    output.write(data)
                    digest.update(data)
                output.flush()
                os.fsync(output.fileno())
            after = os.fstat(input_file.fileno())
        if fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(
            source.stat(follow_symlinks=False)
        ):
            raise ValueError(f"不稳定文件，复制期间发生修改: {relative}")
        destination.chmod(0o400)
        return {
            "logical_path": relative, "snapshot_path": name, "size_bytes": before.st_size,
            "mtime_ns": before.st_mtime_ns, "sha256": digest.hexdigest(),
            "chunk_size": chunk_size, "base_version_id": base_version_id,
            "idempotency_key": uuid.uuid4().hex, "upload_id": None,
        }
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
