FROM ghcr.io/astral-sh/uv:0.12.11 AS uv
FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY syncvault ./syncvault
RUN uv sync --frozen --no-dev --no-editable
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --uid 10001 --create-home syncvault && mkdir -p /data && chown syncvault /data
USER syncvault
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn syncvault.server.main:app --host 0.0.0.0 --port 8000 --workers 1"]
