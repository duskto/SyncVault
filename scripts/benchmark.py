"""HTTP payload benchmark. Requires a running server; output is measured, never estimated."""

import argparse
import hashlib
import json
import os
import resource
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from syncvault.client.transport import Remote


def run_case(server, token, size_mib, chunk_mib, concurrency):
    chunk_size = chunk_mib * 1024 * 1024
    size = size_mib * 1024 * 1024
    digest = hashlib.sha256()
    block = b"x" * min(chunk_size, size)
    for offset in range(0, size, chunk_size):
        digest.update(block[:min(chunk_size, size - offset)])
    with Remote(server, token, concurrency) as remote:
        started = time.perf_counter()
        session = remote.request("POST", "/api/v1/uploads", json={
            "namespace": "benchmark", "logical_path": f"{uuid.uuid4().hex}.bin",
            "size_bytes": size, "sha256": digest.hexdigest(), "chunk_size": chunk_size,
        }, headers={"Idempotency-Key": uuid.uuid4().hex}).json()
        upload_id = session["upload_id"]

        def send(index):
            length = min(chunk_size, size - index * chunk_size)
            remote.request(
                "PUT", f"/api/v1/uploads/{upload_id}/chunks/{index}",
                content=block[:length], headers={"Content-Type": "application/octet-stream"},
            )
            return length

        # Deliberately stop after half the blocks, then query authoritative progress.
        total = session["total_chunks"]
        with ThreadPoolExecutor(concurrency) as pool:
            initial_bytes = sum(pool.map(send, range(total // 2)))
        progress = remote.request("GET", f"/api/v1/uploads/{upload_id}").json()
        received = set(progress["received_chunks"])
        resume_started = time.perf_counter()
        with ThreadPoolExecutor(concurrency) as pool:
            resumed_bytes = sum(pool.map(send, (i for i in range(total) if i not in received)))
        remote.request("POST", f"/api/v1/uploads/{upload_id}/complete")
        finished = time.perf_counter()
        return {
            "size_bytes": size, "chunk_size": chunk_size, "concurrency": concurrency,
            "elapsed_seconds": finished - started, "resume_seconds": finished - resume_started,
            "initial_confirmed_bytes": initial_bytes, "resume_confirmed_bytes": resumed_bytes,
            "client_process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "server_peak_rss_kib": None,
            "note": "净负载按成功确认的块统计；不含 HTTP 开销或丢失响应后的重复接收。"
                    "RSS 为此进程累计峰值；服务端内存需独立测量。",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--size-mib", type=int, default=32)
    parser.add_argument("--network-note", required=True)
    parser.add_argument("--output", default="benchmark-results.json")
    args = parser.parse_args()
    if args.size_mib < 1:
        parser.error("size-mib must be positive")
    token = os.environ["SYNCVAULT_TOKEN"]
    result = {"network": args.network_note, "cases": [
        run_case(args.server, token, args.size_mib, chunk, concurrency)
        for chunk in (1, 4, 8) for concurrency in (1, 2, 4)
    ]}
    with open(args.output, "x") as output:
        json.dump(result, output, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
