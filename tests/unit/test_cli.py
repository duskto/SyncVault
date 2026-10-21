import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from syncvault.client.cli import app
from syncvault.client.state import State
from syncvault.client.transport import Remote


class CLITests(unittest.TestCase):
    def test_init_checks_authentication_without_persisting_token(self):
        runner = CliRunner()
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=[] if request.url.path.endswith("files") else {})

        remote = Remote(
            "http://test", "secret-token", client=httpx.Client(
                base_url="http://test", transport=httpx.MockTransport(handler),
                headers={"Authorization": "Bearer secret-token"},
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch("syncvault.client.cli.remote_for", return_value=remote):
                result = runner.invoke(app, ["init", directory, "--server", "http://test"])
            self.assertEqual(result.exit_code, 0, result.output)
            config = (Path(directory) / ".syncvault/config.json").read_text()
            self.assertNotIn("secret-token", config)
            self.assertEqual([request.url.path for request in requests], ["/health", "/api/v1/files"])

    def test_plan_and_status_leave_file_unmodified(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "document"
            source.write_bytes(b"content")
            with State.initialize(root, "http://test", "personal"):
                pass
            planned = runner.invoke(app, ["plan", directory, "--full"])
            self.assertEqual(planned.exit_code, 0, planned.output)
            self.assertIn('"action": "add"', planned.output)
            status = runner.invoke(app, ["status", directory])
            self.assertEqual(status.exit_code, 0, status.output)
            self.assertEqual(source.read_bytes(), b"content")
            with State(root) as state:
                self.assertEqual(state.manifest(), {})
                self.assertEqual(state.pending(), [])

    def test_uninitialized_directory_returns_clear_error(self):
        with tempfile.TemporaryDirectory() as directory:
            result = CliRunner().invoke(app, ["status", directory])
            self.assertEqual(result.exit_code, 1)
            self.assertIn("尚未初始化", result.output)
