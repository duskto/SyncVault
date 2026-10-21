from dataclasses import asdict, dataclass

from syncvault.client.hashing import hash_file
from syncvault.client.scanner import scan


@dataclass(frozen=True)
class Operation:
    logical_path: str
    action: str
    reason: str

    def as_dict(self):
        return asdict(self)


def plan(state, full=False) -> list[Operation]:
    manifest = state.manifest()
    operations = []
    for entry in scan(state.root):
        old = manifest.get(entry.logical_path)
        if entry.skip_reason:
            action, reason = "skip", entry.skip_reason
        elif old is None:
            action, reason = "add", "尚未备份"
        elif full:
            digest = hash_file(state.root / entry.logical_path)
            action = "skip" if digest == old["sha256"] else "modify"
            reason = "完整 SHA-256 比较"
        elif old["size_bytes"] == entry.size_bytes and old["mtime_ns"] == entry.mtime_ns:
            action, reason = "skip", "size/mtime 相同（快速模式，不证明内容相同）"
        else:
            action, reason = "modify", "size/mtime 已变化"
        operations.append(Operation(entry.logical_path, action, reason))
    return operations
