"""Worker health/readiness. Compose --wait hits this; it fails if Postgres is wrong."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Engine


def create_health_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="Order Pipeline Worker")

    def _ping() -> dict[str, str]:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ok"}

    @app.get("/health")
    def health() -> dict[str, str]:
        return _ping()

    @app.get("/ready")
    def ready() -> dict[str, str]:
        return _ping()

    return app
