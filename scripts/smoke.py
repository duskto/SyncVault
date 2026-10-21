"""Real CLI + HTTP acceptance, including abrupt client exit after a confirmed block."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from syncvault.client.state import State
from syncvault.client.transfer import push
from syncvault.client.transport import Remote


def cli(*arguments):
    result = subprocess.run(
        [sys.executable, "-m", "syncvault.client.cli", *map(str, arguments)],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(f"CLI {arguments[0]} failed: {result.stdout}\n{result.stderr}")
    return result.stdout


def interrupt_worker(root):
    def interrupt_after_confirmation(event):
        if event.get("confirmed_bytes"):
            os._exit(24)

    with State(root) as state, Remote(state.config["server"], os.environ["SYNCVAULT_TOKEN"]) as remote:
        push(state, remote, concurrency=1, chunk_size=1024 * 1024,
             progress=interrupt_after_confirmation)


def run(server):
    namespace = f"smoke-{uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="syncvault-smoke-") as directory:
        root = Path(directory) / "documents"
        root.mkdir()
        (root / "empty").touch()
        source = root / "hello.txt"
        source.write_bytes(b"first version\n")
        cli("init", root, "--server", server, "--namespace", namespace)
        cli("plan", root, "--full")
        cli("push", root)
        first = json.loads(cli("versions", "hello.txt", "--root", root))[0]
        source.write_bytes(b"second version\n")
        cli("push", root, "--full")
        assert len(json.loads(cli("versions", "hello.txt", "--root", root))) == 2
        restored = Path(directory) / "restored.txt"
        cli("restore", "hello.txt", "--version", first["version_id"], "--root", root,
            "--output", restored)
        assert restored.read_bytes() == b"first version\n"
        source.unlink()
        cli("push", root)
        assert len(json.loads(cli("versions", "hello.txt", "--root", root))) == 2
        large = root / "large.bin"
        with large.open("wb") as target:
            for index in range(12):
                target.write(bytes([index]) * 1024 * 1024)
        child = subprocess.run([
            sys.executable, "-m", "scripts.smoke", "--interrupt-worker", str(root),
        ], capture_output=True, text=True)
        if child.returncode != 24:
            raise RuntimeError(f"Expected client exit 24, got {child.returncode}: {child.stderr}")
        with State(root) as state, Remote(server, os.environ["SYNCVAULT_TOKEN"]) as remote:
            pending = state.pending()[0]
            info = remote.request("GET", f"/api/v1/uploads/{pending['upload_id']}").json()
            confirmed_before = len(info["received_chunks"])
            assert 0 < confirmed_before < info["total_chunks"]
            events = []
            version = push(state, remote, concurrency=2, progress=events.append)[0]
            resumed_bytes = sum(event.get("confirmed_bytes", 0) for event in events)
            expected_bytes = pending["size_bytes"] - confirmed_before * pending["chunk_size"]
            assert resumed_bytes == expected_bytes
            assert not state.pending()
        large_copy = Path(directory) / "large-restored.bin"
        cli("restore", "large.bin", "--version", version["version_id"], "--root", root,
            "--output", large_copy)
        with large_copy.open("rb") as downloaded:
            assert hashlib.file_digest(downloaded, "sha256").hexdigest() == version["sha256"]
        cli("verify", root, "--full")
        return {
            "namespace": namespace, "status": "passed", "client_exit_code": child.returncode,
            "confirmed_chunks_before_exit": confirmed_before,
            "resume_new_confirmed_bytes": resumed_bytes,
            "file_size": pending["size_bytes"], "restored_sha256": version["sha256"],
            "retained_version_id": version["version_id"],
            "checks": ["empty file", "two historical versions", "restore old version",
                       "local deletion retains history", "abrupt client exit",
                       "resume missing chunks only", "restore full hash", "verify --full"],
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--interrupt-worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.interrupt_worker:
        interrupt_worker(args.interrupt_worker)
        raise RuntimeError("Worker did not upload a block")
    result = run(args.server)
    serialized = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(serialized)
    print(serialized)


if __name__ == "__main__":
    main()
