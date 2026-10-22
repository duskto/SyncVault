import json
from contextlib import contextmanager

import httpx
import pytest
from typer.testing import CliRunner

from syncvault.client.cli import app
from syncvault.client.pending import cancel_pending, export_snapshot, retry_pending
from syncvault.client.snapshot import snapshot
from syncvault.client.state import State
from syncvault.client.transfer import PushFailed, push
from syncvault.client.transport import APIError, Remote
from syncvault.server.models import Upload
from tests.helpers import initialize, upload_all


@pytest.fixture
def local(api, tmp_path):
    root = tmp_path / "client"
    root.mkdir()
    (root / "file").write_bytes(b"original")
    with State.initialize(root, "http://testserver", "personal") as state:
        remote = Remote("http://testserver", "alice-token", client=api, sleeper=lambda _: None)
        yield state, remote


def save_task(state):
    pending = snapshot(state, "file", 4, None)
    state.save_pending(pending)
    return pending


def test_cancel_recovers_lost_init_and_skips_file_without_deleting_snapshot(api, local):
    state, remote = local
    pending = save_task(state)
    upload_id = initialize(api, b"original", path="file",
                           key=pending["idempotency_key"]).json()["upload_id"]
    result = cancel_pending(state, remote, "file")
    assert result["status"] == "paused"
    assert api.get(f"/api/v1/uploads/{upload_id}").json()["status"] == "CANCELLED"
    assert state.snapshot_path(pending["snapshot_path"]).read_bytes() == b"original"
    (state.root / "other").write_bytes(b"unblocked")
    assert len(push(state, remote)) == 1
    assert set(state.manifest()) == {"other"}
    assert state.pending()[0]["paused"]
    assert cancel_pending(state, remote, "file")["status"] == "paused"


def test_rejected_oversize_task_can_be_cancelled_without_creating_session(api, local):
    state, remote = local
    api.app.state.settings.max_file_size = 2
    with pytest.raises(APIError) as error:
        push(state, remote)
    assert error.value.status == 413
    assert state.pending()[0]["upload_id"] is None
    assert cancel_pending(state, remote, "file")["status"] == "paused"
    from sqlalchemy import select
    with api.app.state.sessions() as db:
        assert list(db.scalars(select(Upload))) == []


def test_cancel_reconciles_completed_session_instead_of_losing_commit(api, local):
    state, remote = local
    pending = save_task(state)
    upload_id = upload_all(api, b"original", path="file")
    version = api.post(f"/api/v1/uploads/{upload_id}/complete").json()
    pending["upload_id"] = upload_id
    state.save_pending(pending)
    result = cancel_pending(state, remote, "file")
    assert result["status"] == "completed"
    assert state.manifest()["file"]["last_synced_version"] == version["version_id"]
    assert not state.pending()


def test_cancel_refuses_finalizing_and_preserves_local_state(api, local):
    state, remote = local
    pending = save_task(state)
    pending["upload_id"] = upload_all(api, b"original", path="file")
    state.save_pending(pending)
    with api.app.state.sessions.begin() as db:
        db.get(Upload, pending["upload_id"]).status = "FINALIZING"
    with pytest.raises(ValueError, match="仍在合并"):
        cancel_pending(state, remote, "file")
    assert not state.pending()[0].get("paused")


def test_complete_wins_cancellation_race_and_is_reconciled(api, local, monkeypatch):
    state, remote = local
    pending = save_task(state)
    uid = upload_all(api, b"original", path="file")
    pending["upload_id"] = uid
    state.save_pending(pending)
    original = remote.request

    def complete_before_cancel(method, url, **kwargs):
        if method == "DELETE":
            assert api.post(f"/api/v1/uploads/{uid}/complete").status_code == 200
        return original(method, url, **kwargs)

    monkeypatch.setattr(remote, "request", complete_before_cancel)
    assert cancel_pending(state, remote, "file")["status"] == "completed"
    assert state.manifest()["file"]["last_synced_version"] == uid
    assert not state.pending()


def test_export_no_clobber_and_corruption_never_published(local, tmp_path):
    state, _ = local
    pending = save_task(state)
    output = tmp_path / "exported"
    export_snapshot(state, "file", output)
    assert output.read_bytes() == b"original"
    with pytest.raises(FileExistsError):
        export_snapshot(state, "file", output)
    with pytest.raises(ValueError, match="状态目录"):
        export_snapshot(state, "file", state.directory / "exported")
    source = state.snapshot_path(pending["snapshot_path"])
    source.chmod(0o600)
    source.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="校验失败"):
        export_snapshot(state, "file", tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
    assert output.read_bytes() == b"original"


def test_retry_uses_original_snapshot_and_preserves_archive_after_commit(local, tmp_path):
    state, remote = local
    original = save_task(state)
    cancel_pending(state, remote, "file")
    (state.root / "file").unlink()  # Resume must not depend on the source still existing.
    queued = retry_pending(state, remote, "file")
    assert queued["status"] == "queued"
    assert state.pending()[0]["idempotency_key"] != original["idempotency_key"]
    assert len(push(state, remote)) == 1
    state.cleanup_snapshots()
    assert state.snapshot_path(original["snapshot_path"]).exists()
    export_snapshot(state, "file", tmp_path / "archived", queued["archive_id"])
    assert (tmp_path / "archived").read_bytes() == b"original"


def test_explicit_source_retry_keeps_old_snapshot(local, tmp_path):
    state, remote = local
    old = save_task(state)
    (state.root / "file").write_bytes(b"replacement")
    queued = retry_pending(state, remote, "file", from_source=True)
    assert state.pending()[0]["snapshot_path"] != old["snapshot_path"]
    assert len(push(state, remote)) == 1
    export_snapshot(state, "file", tmp_path / "old", queued["archive_id"])
    assert (tmp_path / "old").read_bytes() == b"original"


def test_missing_snapshot_requires_explicit_source_retry(local):
    state, remote = local
    pending = save_task(state)
    state.snapshot_path(pending["snapshot_path"]).unlink()
    with pytest.raises(ValueError, match="from-source"):
        retry_pending(state, remote, "file")
    assert not state.archived()
    retry_pending(state, remote, "file", from_source=True)
    assert len(push(state, remote)) == 1


def test_conflict_requires_explicit_current_base_and_preserves_both_versions(api, local):
    state, remote = local
    uid = upload_all(api, b"remote winner", path="file")
    winner = api.post(f"/api/v1/uploads/{uid}/complete").json()
    with pytest.raises(APIError) as error:
        push(state, remote)
    assert error.value.code == "VERSION_CONFLICT"
    before = state.pending()[0].copy()
    with pytest.raises(ValueError, match="不是远端当前版本"):
        retry_pending(state, remote, "file", base_version="outdated-version")
    assert state.pending()[0] == before
    retry_pending(state, remote, "file")
    with pytest.raises(APIError) as error:
        push(state, remote)
    assert error.value.code == "VERSION_CONFLICT"  # Never silently rebase.
    retry_pending(state, remote, "file", base_version=winner["version_id"])
    new = push(state, remote)[0]
    assert api.get(f"/api/v1/versions/{new['version_id']}/content").content == b"original"
    assert api.get(f"/api/v1/versions/{winner['version_id']}/content").content == b"remote winner"
    assert state.archived()


def test_failed_retry_keeps_pending_and_removes_only_new_unreferenced_snapshot(local, monkeypatch):
    state, remote = local
    pending = save_task(state)

    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(remote, "request", unavailable)
    with pytest.raises(httpx.ConnectError):
        retry_pending(state, remote, "file", from_source=True)
    assert state.pending() == [pending]
    assert [p.name for p in (state.directory / "snapshots").iterdir()] == [pending["snapshot_path"]]


def test_remote_change_after_explicit_retry_still_conflicts(api, local):
    state, remote = local
    save_task(state)
    uid = upload_all(api, b"first winner", path="file")
    winner = api.post(f"/api/v1/uploads/{uid}/complete").json()
    retry_pending(state, remote, "file", base_version=winner["version_id"])
    uid2 = upload_all(api, b"newer winner", path="file", base=winner["version_id"])
    newer = api.post(f"/api/v1/uploads/{uid2}/complete").json()
    with pytest.raises(APIError) as error:
        push(state, remote)
    assert error.value.code == "VERSION_CONFLICT"
    assert state.pending()[0]["base_version_id"] == winner["version_id"]
    assert remote.find_file("personal", "file")["current_version_id"] == newer["version_id"]


def test_continue_after_bad_snapshot_returns_failure_and_commits_other_files(local):
    state, remote = local
    pending = save_task(state)
    state.snapshot_path(pending["snapshot_path"]).unlink()
    (state.root / "other").write_bytes(b"independent")
    with pytest.raises(PushFailed) as error:
        push(state, remote, continue_on_error=True)
    assert len(error.value.versions) == 1
    assert [item["path"] for item in error.value.failures] == ["file"]
    assert state.pending() == [pending]
    assert set(state.manifest()) == {"other"}


def test_cli_task_commands_and_partial_failure_exit_code(local, monkeypatch, tmp_path):
    state, remote = local

    @contextmanager
    def remote_for(*args):
        yield remote

    monkeypatch.setattr("syncvault.client.cli.remote_for", remote_for)
    runner = CliRunner()
    pending = save_task(state)
    root = ["--root", str(state.root)]
    result = runner.invoke(app, ["pending", "export", "file", "--output", str(tmp_path / "copy"), *root])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "copy").read_bytes() == b"original"
    result = runner.invoke(app, ["pending", "cancel", "file", *root])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "paused"
    result = runner.invoke(app, ["pending", "retry", "file", *root])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["pending", "list", *root])
    assert len(json.loads(result.output)["archived"]) == 1
    state.snapshot_path(pending["snapshot_path"]).unlink()
    (state.root / "other").write_bytes(b"continue")
    result = runner.invoke(app, ["push", str(state.root), "--continue-on-error"])
    assert result.exit_code == 1, result.output
    assert '"failures"' in result.output
    assert set(state.manifest()) == {"other"}
