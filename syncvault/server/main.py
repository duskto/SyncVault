import errno
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from syncvault.server.api import files, uploads, versions
from syncvault.server.config import Settings
from syncvault.server.database import connect_database
from syncvault.server.models import Base
from syncvault.server.storage import Storage
from syncvault.shared.protocol import ProtocolError


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        if not settings.tokens:
            raise RuntimeError("请设置 SYNCVAULT_TOKENS，例如 {\"your-secret\":\"personal\"}")
        engine, sessions = connect_database(settings.database_url)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessions = sessions
        app.state.storage = Storage(settings.storage_root)
        if settings.auto_create:
            Base.metadata.create_all(engine)
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="SyncVault", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(ProtocolError)
    async def protocol_error(_request: Request, error: ProtocolError):
        headers = {"Retry-After": "1"} if error.status == 202 else {}
        if error.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(
            status_code=error.status,
            content={"error": {"code": error.code, "message": error.message, **error.details}},
            headers=headers,
        )

    @app.exception_handler(OSError)
    async def storage_error(_request: Request, error: OSError):
        logging.getLogger("syncvault").exception("Storage operation failed")
        return JSONResponse(
            status_code=507 if error.errno == errno.ENOSPC else 503,
            content={"error": {"code": "STORAGE_ERROR", "message": "存储操作失败，可重试"}},
        )

    @app.get("/health")
    def health():
        with app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}

    for router in (uploads.router, files.router, versions.router):
        app.include_router(router)
    return app


app = create_app()
