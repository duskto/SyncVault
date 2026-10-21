import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from syncvault.client.hashing import hash_file, iter_chunks, reassemble
from syncvault.client.planner import plan
from syncvault.client.scanner import scan
from syncvault.client.snapshot import snapshot
from syncvault.client.state import State
from syncvault.shared.paths import logical_path, namespace


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_chunk_boundaries_and_reassembly(self):
        for size in (0, 1, 8, 9, 16, 31):
            with self.subTest(size=size):
                data = bytes(range(size))
                source = self.root / "source"
                source.write_bytes(data)
                chunks = list(iter_chunks(source, 8))
                self.assertEqual(len(chunks), (size + 7) // 8)
                result = self.root / "result"
                actual_size, digest = reassemble(chunks, result)
                self.assertEqual(result.read_bytes(), data)
                self.assertEqual(actual_size, size)
                self.assertEqual(digest, hashlib.sha256(data).hexdigest())
                self.assertEqual(hash_file(source), digest)

    def test_invalid_chunk_size(self):
        with self.assertRaises(ValueError):
            list(iter_chunks(self.root / "unused", 0))

    def test_reject_unsafe_paths(self):
        for name in ("", "/etc/passwd", "../secret", "a/../b", "a//b", "a\x00b",
                     "a\\b", "C:/a", "CON", "lpt1.txt", "a.", "a ", "a/./b", "e\u0301"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                logical_path(name)
        self.assertEqual(logical_path("报告/文件.txt"), "报告/文件.txt")
        self.assertEqual(namespace("personal-v1"), "personal-v1")
        with self.assertRaises(ValueError):
            namespace("../escape")

    def test_scanner_exclusions_and_case_collision(self):
        (self.root / "normal").write_bytes(b"content")
        (self.root / "temp.tmp").touch()
        (self.root / ".syncvault").mkdir()
        (self.root / "link").symlink_to(self.root / "normal")
        os.mkfifo(self.root / "pipe")
        result = {entry.logical_path: entry for entry in scan(self.root)}
        self.assertIsNone(result["normal"].skip_reason)
        for name in ("temp.tmp", ".syncvault", "link", "pipe"):
            self.assertIsNotNone(result[name].skip_reason)
        (self.root / "NORMAL").touch()
        with self.assertRaises(ValueError):
            scan(self.root)

    def test_full_mode_detects_restored_mtime(self):
        source = self.root / "document"
        source.write_bytes(b"old")
        original = source.stat()
        with State.initialize(self.root, "http://localhost:8000", "personal") as state:
            pending = snapshot(state, "document", 8, None)
            state.save_pending(pending)
            state.committed(pending, {"version_id": "v1"})
            source.write_bytes(b"new")
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
            quick = {item.logical_path: item.action for item in plan(state)}
            full = {item.logical_path: item.action for item in plan(state, full=True)}
            self.assertEqual(quick["document"], "skip")
            self.assertEqual(full["document"], "modify")

    def test_snapshots_stay_stable_after_source_change(self):
        source = self.root / "document"
        source.write_bytes(b"old")
        with State.initialize(self.root, "http://localhost:8000", "personal") as state:
            pending = snapshot(state, "document", 8, None)
            state.save_pending(pending)
            source.write_bytes(b"new")
            self.assertEqual(state.snapshot_path(pending["snapshot_path"]).read_bytes(), b"old")
        with State(self.root) as state:
            self.assertEqual(state.pending()[0]["sha256"], hashlib.sha256(b"old").hexdigest())
            state.cleanup_snapshots()
            self.assertTrue(state.snapshot_path(pending["snapshot_path"]).exists())

    def test_snapshot_rejects_symlink(self):
        (self.root / "link").symlink_to("/etc/passwd")
        with State.initialize(self.root, "http://localhost:8000", "personal") as state:
            with self.assertRaises(ValueError):
                snapshot(state, "link", 8, None)

    def test_snapshot_detects_changes_during_copy(self):
        source = self.root / "document"
        source.write_bytes(b"old")
        original = os.fsync

        def change_source(fd):
            original(fd)
            source.write_bytes(b"different")

        with State.initialize(self.root, "http://localhost:8000", "personal") as state:
            with patch("syncvault.client.snapshot.os.fsync", side_effect=change_source):
                with self.assertRaisesRegex(ValueError, "不稳定"):
                    snapshot(state, "document", 8, None)
            self.assertEqual(list((state.directory / "snapshots").iterdir()), [])

    def test_state_rejects_reinitialization_with_other_server(self):
        with State.initialize(self.root, "http://localhost:8000", "personal"):
            pass
        with self.assertRaises(ValueError):
            State.initialize(self.root, "http://localhost:8001", "personal")

    def test_state_rejects_symlink_directory(self):
        other = self.root / "other"
        other.mkdir()
        (self.root / ".syncvault").symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError):
            State.initialize(self.root, "http://localhost:8000", "personal")

    def test_client_lock(self):
        with State.initialize(self.root, "http://localhost:8000", "personal") as first:
            with State(self.root) as second, first.lock():
                with self.assertRaises(ValueError), second.lock():
                    pass

    def test_deleting_source_retains_manifest(self):
        (self.root / "a").write_bytes(b"a")
        with State.initialize(self.root, "http://localhost:8000", "personal") as state:
            pending = snapshot(state, "a", 8, None)
            state.committed(pending, {"version_id": "v1"})
            (self.root / "a").unlink()
            self.assertFalse(any(op.action == "delete" for op in plan(state)))
            self.assertIn("a", state.manifest())


if __name__ == "__main__":
    unittest.main()
