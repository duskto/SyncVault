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


def conflict_demo(server, directory):
    """Two clients share a base; explicit retry retains both remote versions and the snapshot."""
    namespace = f"conflict-{uuid.uuid4().hex[:12]}"
    roots = [Path(directory) / name for name in ("writer-a", "writer-b")]
    for root in roots:
        root.mkdir()
        (root / "shared.txt").write_bytes(b"common base")
        cli("init", root, "--server", server, "--namespace", namespace)
    cli("push", roots[0])
    # Seed the second client's known base, as if it had backed up the same version earlier.
    with State(roots[0]) as first, State(roots[1]) as second:
        saved = first.manifest()["shared.txt"]
        second.committed({"logical_path": "shared.txt", "size_bytes": saved["size_bytes"],
                          "mtime_ns": (roots[1] / "shared.txt").stat().st_mtime_ns,
                          "sha256": saved["sha256"]},
                         {"version_id": saved["last_synced_version"]})
    (roots[0] / "shared.txt").write_bytes(b"winner")
    (roots[1] / "shared.txt").write_bytes(b"conflicting snapshot")
    cli("push", roots[0], "--full")
    (roots[1] / "independent.txt").write_bytes(b"independent upload")
    rejected = subprocess.run([
        sys.executable, "-m", "syncvault.client.cli", "push", str(roots[1]),
        "--full", "--continue-on-error",
    ], capture_output=True, text=True)
    assert rejected.returncode == 1 and "VERSION_CONFLICT" in rejected.stdout
    with State(roots[1]) as state:
        assert "independent.txt" in state.manifest()
        assert len(state.pending()) == 1
    output = Path(directory) / "conflict-copy.txt"
    cli("pending", "export", "shared.txt", "--root", roots[1], "--output", output)
    assert output.read_bytes() == b"conflicting snapshot"
    cli("pending", "cancel", "shared.txt", "--root", roots[1])
    cli("push", roots[1])  # Paused paths are not silently resubmitted.
    with Remote(server, os.environ["SYNCVAULT_TOKEN"]) as remote:
        winner = remote.find_file(namespace, "shared.txt")["current_version_id"]
    cli("pending", "retry", "shared.txt", "--root", roots[1], "--base-version", winner)
    cli("push", roots[1])
    history = json.loads(cli("versions", "shared.txt", "--root", roots[1]))
    assert len(history) == 3
    winner_copy = Path(directory) / "winner.txt"
    cli("restore", "shared.txt", "--root", roots[1], "--version", winner,
        "--output", winner_copy)
    assert winner_copy.read_bytes() == b"winner"
    tasks = json.loads(cli("pending", "list", "--root", roots[1]))
    assert not tasks["pending"] and len(tasks["archived"]) == 1
    cli("pending", "export", "shared.txt", "--root", roots[1],
        "--archive", tasks["archived"][0]["archive_id"],
        "--output", Path(directory) / "archived.txt")
    assert (Path(directory) / "archived.txt").read_bytes() == b"conflicting snapshot"
    cli("verify", roots[1], "--full", "--remote-content")
    return {"namespace": namespace, "status": "passed", "history_versions": len(history),
            "conflict_exit_code": rejected.returncode, "archive_export_verified": True}


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
        cli("verify", root, "--full", "--remote-content")
        conflict = conflict_demo(server, directory)
        return {
            "namespace": namespace, "status": "passed", "client_exit_code": child.returncode,
            "confirmed_chunks_before_exit": confirmed_before,
            "resume_new_confirmed_bytes": resumed_bytes,
            "file_size": pending["size_bytes"], "restored_sha256": version["sha256"],
            "retained_version_id": version["version_id"],
            "conflict": conflict,
            "checks": ["empty file", "two historical versions", "restore old version",
                       "local deletion retains history", "abrupt client exit",
                       "resume missing chunks only", "restore full hash", "verify --full",
                       "remote content hash", "conflict and explicit resolution",
                       "cancel retains snapshot", "archive export", "continue after conflict"],
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
