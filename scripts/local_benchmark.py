"""Repeatable local split/reassembly measurement, independent of HTTP dependencies."""

import argparse
import hashlib
import json
import platform
import random
import resource
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from syncvault.client.hashing import hash_file, iter_chunks, reassemble


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mib", type=int, default=16)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.size_mib < 1:
        parser.error("size-mib must be positive")
    result = {
        "kind": "local_split_reassembly_only", "python": platform.python_version(),
        "platform": platform.platform(), "measured_at": datetime.now(timezone.utc).isoformat(),
        "network": "none", "cases": [],
    }
    with tempfile.TemporaryDirectory(prefix="syncvault-local-benchmark-") as directory:
        root = Path(directory)
        source = root / "source"
        randomizer = random.Random(42)
        expected = hashlib.sha256()
        with source.open("wb") as output:
            for _ in range(args.size_mib):
                data = randomizer.randbytes(1024 * 1024)
                output.write(data)
                expected.update(data)
        for chunk_mib in (1, 4, 8):
            started = time.perf_counter()
            size, digest = reassemble(iter_chunks(source, chunk_mib * 1024 * 1024), root / "copy")
            elapsed = time.perf_counter() - started
            assert size == args.size_mib * 1024 * 1024
            assert digest == expected.hexdigest() == hash_file(root / "copy")
            result["cases"].append({
                "size_bytes": size, "chunk_size": chunk_mib * 1024 * 1024,
                "elapsed_seconds": elapsed, "sha256": digest,
                "process_cumulative_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            })
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
