from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from syncvault.server.models import Chunk, File, Upload, Version, now
from tests.helpers import initialize, put, upload_all


@pytest.mark.parametrize("data", [b"", b"a", b"abcd", b"abcde", b"abcdefghijkl"])
def test_boundaries_roundtrip_and_repeated_complete(api, data):
    upload_id = upload_all(api, data)
    response = api.post(f"/api/v1/uploads/{upload_id}/complete")
    assert response.status_code == 200, response.text
    version = response.json()
    assert api.get(f"/api/v1/versions/{version['version_id']}/content").content == data
    assert api.post(f"/api/v1/uploads/{upload_id}/complete").json() == version
    with api.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Version)) == 1
        assert db.get(Upload, upload_id).status == "COMPLETED"
        stored = db.get(Version, version["version_id"])
        assert api.app.state.storage.path(stored.object_key).read_bytes() == data


def test_out_of_order_duplicate_conflicting_and_missing_chunks(api):
    upload_id = initialize(api, b"abcdefghij").json()["upload_id"]
    assert put(api, upload_id, 2, b"ij").status_code == 200
    assert put(api, upload_id, 0, b"abcd").status_code == 200
    assert put(api, upload_id, 0, b"abcd").status_code == 200
    assert put(api, upload_id, 0, b"xxxx").status_code == 409
    assert put(api, upload_id, 3, b"x").status_code == 422
    assert put(api, upload_id, 1, b"short").status_code == 422
    missing = api.post(f"/api/v1/uploads/{upload_id}/complete")
    assert missing.status_code == 409
    assert missing.json()["error"]["missing_chunks"] == [1]
    with api.app.state.sessions() as db:
        assert db.get(Upload, upload_id).status == "OPEN"
        assert db.scalar(select(func.count()).select_from(Version)) == 0
    assert put(api, upload_id, 1, b"efgh").status_code == 200
    assert api.post(f"/api/v1/uploads/{upload_id}/complete").status_code == 200


def test_idempotency_binding_and_concurrent_initialization(api):
    with ThreadPoolExecutor(4) as executor:
        responses = list(executor.map(lambda _: initialize(api, b"abcd", key="key"), range(4)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["upload_id"] for response in responses}) == 1
    assert initialize(api, b"other", key="key").status_code == 409


def test_idempotency_lookup_is_read_only_and_owner_scoped(api):
    initialized = initialize(api, b"bytes", key="lost-receipt").json()
    query = {"idempotency_key": "lost-receipt"}
    result = api.get("/api/v1/uploads", params=query)
    assert result.status_code == 200
    assert result.json()["upload_id"] == initialized["upload_id"]
    assert api.get("/api/v1/uploads", params=query,
                   headers={"Authorization": "Bearer bob-token"}).status_code == 404
    assert api.get("/api/v1/uploads", params={"idempotency_key": "missing"}).status_code == 404
    assert api.get("/api/v1/uploads", params={"idempotency_key": ""}).status_code == 422
    with api.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Upload)) == 1


def test_concurrent_duplicate_blocks_are_unique(api):
    upload_id = initialize(api, b"abcd").json()["upload_id"]
    with ThreadPoolExecutor(4) as executor:
        responses = list(executor.map(lambda _: put(api, upload_id, 0, b"abcd"), range(4)))
    assert all(response.status_code == 200 for response in responses)
    with api.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Chunk)) == 1


def test_version_conflict_preserves_previous_version(api):
    first = upload_all(api, b"original")
    original = api.post(f"/api/v1/uploads/{first}/complete").json()
    second = upload_all(api, b"second", base=original["version_id"])
    third = upload_all(api, b"third", base=original["version_id"])
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(
            lambda uid: api.post(f"/api/v1/uploads/{uid}/complete"), [second, third]
        ))
    assert sorted(response.status_code for response in results) == [200, 409]
    assert api.get(f"/api/v1/versions/{original['version_id']}/content").content == b"original"
    with api.app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Version)) == 2
        statuses = {db.get(Upload, uid).status for uid in (second, third)}
        assert statuses == {"COMPLETED", "FAILED"}
        file = db.get(File, original["file_id"])
        assert file.current_version_id != original["version_id"]


def test_owner_isolation_for_every_resource(api):
    upload_id = upload_all(api, b"secret")
    headers = {"Authorization": "Bearer bob-token"}
    assert api.get(f"/api/v1/uploads/{upload_id}", headers=headers).status_code == 404
    assert api.delete(f"/api/v1/uploads/{upload_id}", headers=headers).status_code == 404
    assert api.post(f"/api/v1/uploads/{upload_id}/complete", headers=headers).status_code == 404
    assert api.put(f"/api/v1/uploads/{upload_id}/chunks/0", content=b"xxxx",
                   headers=headers).status_code == 404
    version = api.post(f"/api/v1/uploads/{upload_id}/complete").json()
    history = api.get(f"/api/v1/files/{version['file_id']}/versions", headers=headers)
    assert history.status_code == 404
    assert api.get(f"/api/v1/versions/{version['version_id']}/content",
                   headers=headers).status_code == 404
    assert api.get("/api/v1/files?namespace=personal", headers=headers).json() == []
    assert api.get("/api/v1/files?namespace=personal",
                   headers={"Authorization": ""}).status_code == 401


@pytest.mark.parametrize("path", ["../x", "/x", "a\\b", "a\x00b", "CON", "e\u0301"])
def test_invalid_paths(api, path):
    assert initialize(api, b"data", path=path).status_code == 422


def test_case_collision_expiration_and_cancel(api):
    upload_id = initialize(api, b"a", path="Report").json()["upload_id"]
    assert initialize(api, b"a", path="report").status_code == 409
    with api.app.state.sessions.begin() as db:
        db.get(Upload, upload_id).expires_at = now() - 1
    assert api.get(f"/api/v1/uploads/{upload_id}").json()["status"] == "EXPIRED"
    assert put(api, upload_id, 0, b"a").status_code == 410
    other = initialize(api, b"a", path="other").json()["upload_id"]
    assert api.delete(f"/api/v1/uploads/{other}").status_code == 200
    assert api.delete(f"/api/v1/uploads/{other}").status_code == 200
    assert put(api, other, 0, b"a").status_code == 409


def test_directory_spelling_and_file_directory_collisions(api):
    assert initialize(api, b"a", path="Reports/a").status_code == 200
    assert initialize(api, b"b", path="reports/b").status_code == 409
    assert initialize(api, b"c", path="Reports").status_code == 409
    assert initialize(api, b"d", path="Reports/a/child").status_code == 409


def test_wrong_whole_file_hash_never_publishes(api):
    upload_id = initialize(api, b"abcd").json()["upload_id"]
    assert put(api, upload_id, 0, b"xxxx").status_code == 200
    assert api.post(f"/api/v1/uploads/{upload_id}/complete").status_code == 422
    with api.app.state.sessions() as db:
        assert db.get(Upload, upload_id).status == "FAILED"
        assert db.scalar(select(func.count()).select_from(Version)) == 0
    assert list(api.app.state.storage.path("objects").iterdir()) == []
