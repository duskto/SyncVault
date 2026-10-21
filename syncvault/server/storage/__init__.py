import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

from syncvault.shared.protocol import ProtocolError


class Storage:
    """Local POSIX store. Locks coordinate API workers and the recovery process."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        for name in ("chunks", "objects", "tmp", "locks"):
            (self.root / name).mkdir(mode=0o700, parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("Storage key escaped root")
        return candidate

    @contextmanager
    def lock(self, key: str, blocking: bool = True):
        # Lock files must never be unlinked: doing so creates two lock domains.
        with self.path(f"locks/{key}.lock").open("ab") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as error:
                raise ProtocolError(202, "FINALIZING", "版本仍在合并，请稍后重试") from error
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def publish(self, source: Path, key: str):
        destination = self.path(key)
        os.replace(source, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
