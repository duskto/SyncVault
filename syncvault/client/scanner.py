import os
from dataclasses import dataclass
from pathlib import Path

from syncvault.shared.paths import logical_path, path_key


@dataclass(frozen=True)
class Entry:
    logical_path: str
    size_bytes: int = 0
    mtime_ns: int = 0
    skip_reason: str | None = None


def scan(root: Path) -> list[Entry]:
    result = []
    seen = {}

    def walk(directory):
        # Any unreadable directory aborts the scan; absence is never interpreted as deletion.
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            relative = Path(entry.path).relative_to(root).as_posix()
            if entry.name == ".syncvault":
                result.append(Entry(relative, skip_reason="客户端状态目录"))
                continue
            if entry.is_symlink():
                result.append(Entry(relative, skip_reason="符号链接"))
                continue
            if entry.name.endswith((".tmp", ".part", ".swp", "~")):
                result.append(Entry(relative, skip_reason="临时文件"))
                continue
            logical_path(relative)
            key = path_key(relative)
            if key in seen and seen[key] != relative:
                raise ValueError(f"路径大小写冲突: {seen[key]} / {relative}")
            seen[key] = relative
            if entry.is_dir(follow_symlinks=False):
                walk(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                stat = entry.stat(follow_symlinks=False)
                result.append(Entry(relative, stat.st_size, stat.st_mtime_ns))
            else:
                result.append(Entry(relative, skip_reason="非普通文件"))

    walk(root)
    return result
