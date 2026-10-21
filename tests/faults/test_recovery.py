import errno
import multiprocessing
import os

import pytest
from sqlalchemy import func, select

from syncvault.client.restore import restore
from syncvault.client.snapshot import snapshot
from syncvault.client.state import State
from syncvault.client.transfer import push, upload_pending
from syncvault.client.transport import Remote
from syncvault.server.database import connect_database
from syncvault.server.models import Chunk, Upload, Version, now
from syncvault.server.recovery import cleanup, recover
from syncvault.server.services import finalize
from syncvault.server.storage import Storage
from tests.helpers import initialize, put, upload_all


def crash_after_object(database_url, root, upload_id):
    engine, sessions = connect_database(database_url)
    finalize.publish_metadata = lambda *_: os._exit(23)
    finalize.complete(sessions, Storage(root), "alice", upload_id)
    engine.dispose()


def test_process_dies_between_object_publish_and_database_commit(api):
    upload_id = upload_all(api, b"recover me")
    state = api.app.state
    process = multiprocessing.get_context("spawn").Process(
        target=crash_after_object,
        args=(state.settings.database_url, state.settings.storage_root, upload_id),
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail("crash worker timed out")
    assert process.exitcode == 23
    with state.sessions() as db:
        assert db.get(Upload, upload_id).status == "FINALIZING"
        assert db.scalar(select(func.count()).select_from(Version)) == 0
    assert state.storage.path(f"objects/{upload_id}").read_bytes() == b"recover me"
    result = recover(state.sessions, state.storage)
    assert result[0]["version"]["version_id"] == upload_id
    assert api.post(f"/api/v1/uploads/{upload_id}/complete").json()["version_id"] == upload_id
    with state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Version)) == 1


def test_disk_full_before_object_publish_never_exposes_partial_version(api, monkeypatch):
    upload_id = upload_all(api, b"safe bytes")
    state = api.app.state
    original = state.storage.publish

    def disk_full(source, key):
        if key.startswith("objects/"):
            raise OSError(errno.ENOSPC, "disk full")
        return original(source, key)

    monkeypatch.setattr(state.storage, "publish", disk_full)
    response = api.post(f"/api/v1/uploads/{upload_id}/complete")
    assert response.status_code == 507
    with state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Version)) == 0
        assert db.get(Upload, upload_id).status == "FINALIZING"
    assert list(state.storage.path("objects").iterdir()) == []
    monkeypatch.setattr(state.storage, "publish", original)
    assert recover(state.sessions, state.storage)[0]["version"]["size_bytes"] == 10


def test_corrupt_block_is_detected_even_when_hash_metadata_matches(api):
    upload_id = upload_all(api, b"original")
    with api.app.state.sessions() as db:
        chunk = db.get(Chunk, (upload_id, 0))
        api.app.state.storage.path(chunk.storage_key).write_bytes(b"xxxx")
    assert api.post(f"/api/v1/uploads/{upload_id}/complete").status_code == 422
    with api.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Version)) == 0


def test_restart_only_sends_missing_blocks_and_preserves_snapshot(api, tmp_path, monkeypatch):
    root = tmp_path / "client"
    root.mkdir()
    (root / "file").write_bytes(b"abcdefghijkl")
    with State.initialize(root, "http://testserver", "personal") as state:
        pending = snapshot(state, "file", 4, None)
        pending["upload_id"] = initialize(api, b"abcdefghijkl", path="file").json()["upload_id"]
        state.save_pending(pending)
        put(api, pending["upload_id"], 0, b"abcd")
    # Source mutation after interruption must not change what the old session receives.
    (root / "file").write_bytes(b"source changed")
    original_request = api.request
    transmitted = []

    def tracked_request(method, url, **kwargs):
        if method == "PUT":
            transmitted.append((str(url), len(kwargs["content"])))
        return original_request(method, url, **kwargs)

    monkeypatch.setattr(api, "request", tracked_request)
    remote = Remote("http://testserver", "alice-token", client=api, sleeper=lambda _: None)
    with State(root) as state:
        version = upload_pending(state, remote, state.pending()[0], concurrency=2)
        assert state.pending() == []
        assert state.manifest()["file"]["last_synced_version"] == version["version_id"]
    assert {url.rsplit("/", 1)[1] for url, _ in transmitted} == {"1", "2"}
    assert sum(size for _, size in transmitted) == 8
    assert api.get(f"/api/v1/versions/{version['version_id']}/content").content == b"abcdefghijkl"


def test_lost_put_response_retries_without_duplicate_metadata(api, tmp_path, monkeypatch):
    import httpx

    root = tmp_path / "client"
    root.mkdir()
    (root / "file").write_bytes(b"abcd")
    original_request = api.request
    attempts = []

    def lost_response(method, url, **kwargs):
        response = original_request(method, url, **kwargs)
        if method == "PUT":
            attempts.append(str(url))
            if len(attempts) == 1:
                raise httpx.ReadError("response lost after successful server commit")
        return response

    monkeypatch.setattr(api, "request", lost_response)
    remote = Remote("http://testserver", "alice-token", client=api, sleeper=lambda _: None)
    with State.initialize(root, "http://testserver", "personal") as state:
        versions = push(state, remote, chunk_size=4)
        assert len(versions) == 1
        assert state.pending() == []
    assert len(attempts) == 2
    with api.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Chunk)) == 1
        assert db.scalar(select(func.count()).select_from(Version)) == 1


def test_modified_backup_and_old_version_restore(api, tmp_path):
    root = tmp_path / "client"
    root.mkdir()
    source = root / "file"
    source.write_bytes(b"first")
    remote = Remote("http://testserver", "alice-token", client=api, sleeper=lambda _: None)
    with State.initialize(root, "http://testserver", "personal") as state:
        first = push(state, remote, chunk_size=4)[0]
        assert push(state, remote) == []
        source.write_bytes(b"second content")
        second = push(state, remote, chunk_size=4)[0]
        assert first["version_id"] != second["version_id"]
        source.unlink()
        assert push(state, remote) == []
        restore(remote, first, tmp_path / "restored")
    assert (tmp_path / "restored").read_bytes() == b"first"
    assert api.get(f"/api/v1/versions/{second['version_id']}/content").content == b"second content"


def test_missing_snapshot_does_not_use_new_source(api, tmp_path):
    root = tmp_path / "client"
    root.mkdir()
    (root / "file").write_bytes(b"old")
    remote = Remote("http://testserver", "alice-token", client=api, sleeper=lambda _: None)
    with State.initialize(root, "http://testserver", "personal") as state:
        pending = snapshot(state, "file", 4, None)
        state.save_pending(pending)
        state.snapshot_path(pending["snapshot_path"]).unlink()
        (root / "file").write_bytes(b"new")
        with pytest.raises(ValueError, match="快照丢失"):
            upload_pending(state, remote, pending)
        assert len(state.pending()) == 1
        assert state.manifest() == {}


def test_cleanup_retains_published_objects_and_removes_expired_blocks(api):
    upload_id = upload_all(api, b"keep")
    version = api.post(f"/api/v1/uploads/{upload_id}/complete").json()
    expired = upload_all(api, b"expire", path="other")
    with api.app.state.sessions.begin() as db:
        db.get(Upload, upload_id).updated_at = now() - 7200
        db.get(Upload, expired).expires_at = now() - 7200
        db.get(Upload, expired).updated_at = now() - 7200
    cleanup(api.app.state.sessions, api.app.state.storage, grace_seconds=3600)
    assert api.get(f"/api/v1/versions/{version['version_id']}/content").content == b"keep"
    with api.app.state.sessions() as db:
        assert db.get(Upload, expired).status == "EXPIRED"
        assert db.scalar(select(func.count()).select_from(Chunk)) == 0


def test_lost_complete_response_commits_one_version(api, tmp_path, monkeypatch):
    import httpx

    root = tmp_path / "client"
    root.mkdir()
    (root / "file").write_bytes(b"complete once")
    original_request = api.request
    attempts = []

    def lost_response(method, url, **kwargs):
        response = original_request(method, url, **kwargs)
        if method == "POST" and str(url).endswith("/complete"):
            attempts.append(str(url))
            if len(attempts) == 1:
                raise httpx.ReadError("response lost after database commit")
        return response

    monkeypatch.setattr(api, "request", lost_response)
    remote = Remote("http://testserver", "alice-token", client=api, sleeper=lambda _: None)
    with State.initialize(root, "http://testserver", "personal") as state:
        version = push(state, remote, chunk_size=4)[0]
        assert not state.pending()
        assert state.manifest()["file"]["last_synced_version"] == version["version_id"]
    assert len(attempts) == 2
    with api.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Version)) == 1


def test_expired_upload_restarts_with_same_snapshot(api, tmp_path):
    root = tmp_path / "client"
    root.mkdir()
    (root / "file").write_bytes(b"original")
    remote = Remote("http://testserver", "alice-token", client=api, sleeper=lambda _: None)
    with State.initialize(root, "http://testserver", "personal") as state:
        pending = snapshot(state, "file", 4, None)
        old_id = initialize(api, b"original", path="file").json()["upload_id"]
        pending["upload_id"] = old_id
        state.save_pending(pending)
        put(api, old_id, 0, b"orig")
        with api.app.state.sessions.begin() as db:
            db.get(Upload, old_id).expires_at = now() - 1
        (root / "file").write_bytes(b"changed")
        version = upload_pending(state, remote, pending)
        assert version["upload_id"] != old_id
        assert not state.pending()
        assert api.get(f"/api/v1/versions/{version['version_id']}/content").content == b"original"


def test_concurrent_complete_reports_in_progress(api, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    upload_id = upload_all(api, b"only once")
    reached, release = Event(), Event()
    original = finalize.publish_metadata

    def pause_before_commit(*args):
        reached.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(finalize, "publish_metadata", pause_before_commit)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(api.post, f"/api/v1/uploads/{upload_id}/complete")
        try:
            assert reached.wait(10)
            second = api.post(f"/api/v1/uploads/{upload_id}/complete")
            assert second.status_code == 202
            assert second.headers["Retry-After"] == "1"
        finally:
            release.set()
        assert first.result().status_code == 200
    with api.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Version)) == 1
