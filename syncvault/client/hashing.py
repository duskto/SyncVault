import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path


def iter_chunks(path: Path, chunk_size: int = 4 * 1024 * 1024) -> Iterator[bytes]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    with path.open("rb") as source:
        while data := source.read(chunk_size):
            yield data


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    for data in iter_chunks(path):
        digest.update(data)
    return digest.hexdigest()


def reassemble(chunks: Iterable[bytes], output: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with output.open("wb") as target:
        for data in chunks:
            target.write(data)
            digest.update(data)
            size += len(data)
    return size, digest.hexdigest()
