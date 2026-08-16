FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY docker-entrypoint.sh /docker-entrypoint.sh

RUN uv sync --frozen --no-dev --no-editable \
    && chmod +x /docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000 8081 8082 8083

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["uvicorn", "order_pipeline.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
