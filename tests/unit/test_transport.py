import hashlib
import tempfile
import unittest
from pathlib import Path

import httpx

from syncvault.client.restore import restore
from syncvault.client.transport import APIError, Remote


class TransportTests(unittest.TestCase):
    def remote(self, handler, delays=None):
        client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
        remote = Remote("http://test", "token", client=client, sleeper=(delays or []).append)
        self.addCleanup(client.close)
        return remote

    def test_retry_lost_response(self):
        attempts = []

        def handler(request):
            attempts.append(request.content)
            if len(attempts) == 1:
                raise httpx.ReadError("lost response")
            return httpx.Response(200, json={"ok": True})

        remote = self.remote(handler)
        self.assertEqual(remote.request("PUT", "/chunk", content=b"bytes").status_code, 200)
        self.assertEqual(attempts, [b"bytes", b"bytes"])

    def test_conflict_is_not_retried(self):
        attempts = []

        def handler(request):
            attempts.append(request)
            return httpx.Response(409, json={"error": {"code": "VERSION_CONFLICT"}})

        with self.assertRaises(APIError):
            self.remote(handler).request("POST", "/complete")
        self.assertEqual(len(attempts), 1)

    def test_retry_after(self):
        delays, attempts = [], []

        def handler(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200)

        remote = self.remote(handler)
        remote.sleep = delays.append
        remote.request("GET", "/content")
        self.assertEqual(delays, [2])

    def test_retry_limit(self):
        attempts = []

        def handler(request):
            attempts.append(request)
            return httpx.Response(503)

        with self.assertRaises(APIError):
            self.remote(handler).request("GET", "/content")
        self.assertEqual(len(attempts), 6)

    def test_restore_hash_failure_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "target"
            output.write_bytes(b"original")
            remote = self.remote(lambda _: httpx.Response(200, content=b"bad"))
            version = {"version_id": "v", "size_bytes": 3,
                       "sha256": hashlib.sha256(b"new").hexdigest()}
            with self.assertRaises(ValueError):
                restore(remote, version, output, overwrite=True)
            self.assertEqual(output.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_restore_no_clobber_and_correct_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "target"
            remote = self.remote(lambda _: httpx.Response(200, content=b"new"))
            version = {"version_id": "v", "size_bytes": 3,
                       "sha256": hashlib.sha256(b"new").hexdigest()}
            restore(remote, version, output)
            self.assertEqual(output.read_bytes(), b"new")
            with self.assertRaises(FileExistsError):
                restore(remote, version, output)

    def test_interrupted_download_restarts_whole_version(self):
        class BrokenStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"part"
                raise httpx.ReadError("interrupted")

        attempts = []

        def handler(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(200, stream=BrokenStream())
            return httpx.Response(200, content=b"whole")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "target"
            remote = self.remote(handler)
            version = {"version_id": "v", "size_bytes": 5,
                       "sha256": hashlib.sha256(b"whole").hexdigest()}
            restore(remote, version, output)
            self.assertEqual(output.read_bytes(), b"whole")
            self.assertEqual(len(attempts), 2)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_restore_concurrent_creator_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "target"

            def handler(_):
                output.write_bytes(b"other writer")
                return httpx.Response(200, content=b"new")

            remote = self.remote(handler)
            version = {"version_id": "v", "size_bytes": 3,
                       "sha256": hashlib.sha256(b"new").hexdigest()}
            with self.assertRaises(FileExistsError):
                restore(remote, version, output)
            self.assertEqual(output.read_bytes(), b"other writer")
