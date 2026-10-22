import json
from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from syncvault.client.cli import app
from syncvault.client.state import State
from syncvault.client.transfer import push
from syncvault.client.transport import Remote


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_verify_remote_bytes_detects_damage_while_metadata_matches(api, tmp_path, monkeypatch, damage):
    root = tmp_path / "documents"
    root.mkdir()
    (root / "file").write_bytes(b"original")
    remote = Remote("http://testserver", "alice-token", client=api, sleeper=lambda _: None)
    with State.initialize(root, "http://testserver", "personal") as state:
        version = push(state, remote)[0]
    path = api.app.state.storage.path(f"objects/{version['upload_id']}")
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"modified")  # Same length, different hash.

    @contextmanager
    def remote_for(*args):
        yield remote

    monkeypatch.setattr("syncvault.client.cli.remote_for", remote_for)
    runner = CliRunner()
    metadata_only = runner.invoke(app, ["verify", str(root), "--full"])
    assert metadata_only.exit_code == 0, metadata_only.output
    content = runner.invoke(app, ["verify", str(root), "--full", "--remote-content"])
    assert content.exit_code == 1, content.output
    report = json.loads(content.output)
    assert report["remote_content_checked"] == 0
    assert report["problems"][0]["path"] == "file"
    assert "远端内容检查失败" in report["problems"][0]["reason"]
    assert (root / "file").read_bytes() == b"original"


def test_remote_check_includes_current_files_missing_locally(api, tmp_path, monkeypatch):
    root = tmp_path / "documents"
    root.mkdir()
    (root / "file").write_bytes(b"preserved")
    remote = Remote("http://testserver", "alice-token", client=api, sleeper=lambda _: None)
    with State.initialize(root, "http://testserver", "personal") as state:
        push(state, remote)
    (root / "file").unlink()

    @contextmanager
    def remote_for(*args):
        yield remote

    monkeypatch.setattr("syncvault.client.cli.remote_for", remote_for)
    result = CliRunner().invoke(app, ["verify", str(root), "--remote-content"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["remote_content_checked"] == 1
    assert report["local_missing_remote_retained"] == ["file"]
    assert sorted(p.name for p in root.iterdir()) == [".syncvault"]
