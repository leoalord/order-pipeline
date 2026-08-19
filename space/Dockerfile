# Hugging Face Space image: one container, board on :7860.
# Compose still uses the repo-root Dockerfile + dashboard/Dockerfile.
# Build locally: docker build -f space/Dockerfile -t order-pipeline-space .

FROM python:3.12-slim-bookworm AS python-deps
COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN uv sync --frozen --no-dev --no-editable

FROM node:22-bookworm-slim AS dashboard
WORKDIR /dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql \
        postgresql-client \
        nginx \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 user \
    && mkdir -p /home/user/app /home/user/data /tmp/nginx \
    && chown -R user:user /home/user /tmp/nginx

COPY --from=python-deps --chown=user:user /app /home/user/app
COPY --from=dashboard --chown=user:user /dashboard/dist /home/user/app/dashboard/dist
COPY --chown=user:user space/start.sh space/nginx.conf /home/user/app/space/

RUN chmod +x /home/user/app/space/start.sh

USER user
ENV HOME=/home/user \
    PATH="/home/user/app/.venv/bin:$PATH"

WORKDIR /home/user/app
EXPOSE 7860
CMD ["/home/user/app/space/start.sh"]
