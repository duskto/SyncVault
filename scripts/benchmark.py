"""HTTP payload benchmark. Requires a running server; output is measured, never estimated."""

import argparse
import hashlib
import json
import os
import resource
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

from syncvault.client.transport import Remote


@contextmanager
def measure_server(pid):
    sample = {"server_peak_rss_kib": None, "server_baseline_rss_kib": None,
              "server_rss_samples": 0, "server_memory_error": None}
    if pid is None:
        yield sample
        return
    status = Path(f"/proc/{pid}/status")

    def read():
        text = status.read_text()
        return int(next(line.split()[1] for line in text.splitlines() if line.startswith("VmRSS:")))

    # Reject inaccessible or non-Linux PIDs before starting an upload.
    sample["server_baseline_rss_kib"] = sample["server_peak_rss_kib"] = read()
    sample["server_rss_samples"] = 1
    stopped = Event()

    def observe():
        while not stopped.wait(0.01):
            try:
                sample["server_peak_rss_kib"] = max(sample["server_peak_rss_kib"], read())
                sample["server_rss_samples"] += 1
            except (OSError, StopIteration, ValueError) as error:
                sample["server_memory_error"] = str(error)
                return

    thread = Thread(target=observe, daemon=True)
    thread.start()
    try:
        yield sample
    finally:
        stopped.set()
        thread.join()


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
            "client_pid": os.getpid(),
            "server_peak_rss_kib": None,
            "note": "净负载按成功确认的块统计；不含 HTTP 开销或丢失响应后的重复接收。"
                    "每组使用独立客户端进程；可用 --server-pid 对服务端 VmRSS 分组采样。",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--size-mib", type=int, default=32)
    parser.add_argument("--network-note", required=True)
    parser.add_argument("--output", default="benchmark-results.json")
    parser.add_argument("--server-pid", type=int, help="本机 Linux API 进程 PID，采样每组服务端 RSS")
    parser.add_argument("--case", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--chunk-mib", type=int, choices=(1, 4, 8), help=argparse.SUPPRESS)
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 4), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.size_mib < 1:
        parser.error("size-mib must be positive")
    if args.server_pid is not None and args.server_pid < 1:
        parser.error("server-pid must be positive")
    token = os.environ["SYNCVAULT_TOKEN"]
    if args.case:
        if args.chunk_mib is None or args.concurrency is None:
            parser.error("case requires chunk-mib and concurrency")
        with measure_server(args.server_pid) as memory:
            result = run_case(args.server, token, args.size_mib, args.chunk_mib, args.concurrency)
        result.update(memory)
        if memory["server_memory_error"]:
            raise RuntimeError(f"服务端内存采样失败: {memory['server_memory_error']}")
        print(json.dumps(result, ensure_ascii=False))
        return
    result = {"network": args.network_note, "cases": [],
              "server_memory_note": "每组间隔 10ms 采样 VmRSS；含服务端基础内存，可能漏掉短时峰值。"
                                    "服务端未逐组重启，不是增量内存或进程累计 VmHWM。"}
    with open(args.output, "x") as output:
        for chunk in (1, 4, 8):
            for concurrency in (1, 2, 4):
                command = [sys.executable, "-m", "scripts.benchmark", "--case",
                           "--server", args.server, "--size-mib", str(args.size_mib),
                           "--chunk-mib", str(chunk), "--concurrency", str(concurrency),
                           "--network-note", args.network_note]
                if args.server_pid is not None:
                    command.extend(["--server-pid", str(args.server_pid)])
                completed = subprocess.run(command, check=True, capture_output=True, text=True)
                result["cases"].append(json.loads(completed.stdout))
        json.dump(result, output, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
