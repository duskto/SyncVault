import os

import pytest
from fastapi.testclient import TestClient

from syncvault.server.config import Settings
from syncvault.server.database import connect_database
from syncvault.server.main import create_app
from syncvault.server.models import Base


@pytest.fixture
def api(tmp_path):
    # PostgreSQL CI must point this at a dedicated, disposable test database.
    external = os.getenv("SYNCVAULT_TEST_DATABASE_URL")
    settings = Settings(
        database_url=external or f"sqlite:///{tmp_path / 'server.db'}",
        storage_root=tmp_path / "store", tokens={"alice-token": "alice", "bob-token": "bob"},
        auto_create=True,
    )
    if external:
        engine, _ = connect_database(external)
        Base.metadata.drop_all(engine)
        engine.dispose()
    app = create_app(settings)
    with TestClient(app, headers={"Authorization": "Bearer alice-token"}) as client:
        yield client
    if external:
        engine, _ = connect_database(external)
        Base.metadata.drop_all(engine)
        engine.dispose()
