from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from syncvault.server.database import connect_database


def test_migration_roundtrip_and_model_consistency(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("SYNCVAULT_DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.check(config)
    engine, _ = connect_database(url)
    expected = {"files", "versions", "upload_chunks", "upload_sessions"}
    assert expected.issubset(set(inspect(engine).get_table_names()))
    engine.dispose()
    command.downgrade(config, "base")
    engine, _ = connect_database(url)
    assert not expected.intersection(inspect(engine).get_table_names())
    engine.dispose()
    command.upgrade(config, "head")
    command.check(config)
