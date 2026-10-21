import hashlib


def initialize(api, data, path="file.bin", chunk_size=4, base=None, key=None):
    return api.post("/api/v1/uploads", json={
        "namespace": "personal", "logical_path": path, "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), "chunk_size": chunk_size,
        "base_version_id": base,
    }, headers={"Idempotency-Key": key} if key else {})


def put(api, upload_id, index, data):
    return api.put(f"/api/v1/uploads/{upload_id}/chunks/{index}", content=data, headers={
        "Content-Type": "application/octet-stream",
        "X-Chunk-SHA256": hashlib.sha256(data).hexdigest(),
    })


def upload_all(api, data, path="file.bin", chunk_size=4, base=None):
    response = initialize(api, data, path, chunk_size, base)
    assert response.status_code == 200, response.text
    upload_id = response.json()["upload_id"]
    for index, offset in enumerate(range(0, len(data), chunk_size)):
        result = put(api, upload_id, index, data[offset:offset + chunk_size])
        assert result.status_code == 200, result.text
    return upload_id
