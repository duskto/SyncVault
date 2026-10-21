#!/usr/bin/env bash
set -euo pipefail
: "${SYNCVAULT_TOKEN:?Set SYNCVAULT_TOKEN}"
SYNCVAULT_EXAMPLE_SERVER="${SYNCVAULT_SERVER:-http://localhost:8000}"
SYNCVAULT_EXAMPLE_ID=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')

# Empty-file example: no blocks, but complete is still mandatory.
SYNCVAULT_EXAMPLE_SESSION=$(curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $SYNCVAULT_TOKEN" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: example-$SYNCVAULT_EXAMPLE_ID" \
  --data "{\"namespace\":\"examples\",\"logical_path\":\"empty-$SYNCVAULT_EXAMPLE_ID.txt\",\"size_bytes\":0,\"sha256\":\"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\",\"chunk_size\":4194304}" \
  "$SYNCVAULT_EXAMPLE_SERVER/api/v1/uploads")
SYNCVAULT_EXAMPLE_UPLOAD=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["upload_id"])' \
  <<< "$SYNCVAULT_EXAMPLE_SESSION")
curl --fail-with-body --silent --show-error -X POST \
  -H "Authorization: Bearer $SYNCVAULT_TOKEN" \
  "$SYNCVAULT_EXAMPLE_SERVER/api/v1/uploads/$SYNCVAULT_EXAMPLE_UPLOAD/complete"
