# Order Pipeline

Single-machine order pipeline. One Python package; Compose runs Postgres and the API.

## Run

```bash
uv sync

docker compose down -v
docker compose up --wait
curl -sf http://localhost:8000/health

make check   # uv run ruff + mypy + pytest
```

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`. `make check` is the one command for lint, type-check, and tests. The health integration test fails if the API or Postgres is down.

## Load

Loadgen and dinner-rush profiles land in a later slice.

## Faults

Sim fault admin and crash/outage beats land in later slices.

## Architecture

- One Python package (`order_pipeline`) with room for worker and sim entrypoints; one Dockerfile.
- Runtime and dev deps live in `pyproject.toml`; `uv.lock` pins versions. No `requirements.txt`.
- Compose carries wiring only (DSNs, hosts, ports). Knob defaults live in `APISettings`.
- Alembic is configured with an empty `versions/` directory. The business schema revision is the next slice.

## Trade-offs

- Empty Alembic `versions/` now so the one business-schema revision can land as a single later file.
- Python type-check only in this slice (`tsc` waits for the dashboard).
