"""Console / module entrypoint: `worker` or `python -m order_pipeline.worker`."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from order_pipeline.worker.chassis import Worker
from order_pipeline.worker.deps import DepCaps
from order_pipeline.worker.dispatch import CourierHandlers
from order_pipeline.worker.health import create_health_app
from order_pipeline.worker.http import CourierClient, RestaurantClient
from order_pipeline.worker.kitchen import KitchenHandlers
from order_pipeline.worker.settings import WorkerSettings

# Every off-loop finalize holds a connection, plus claim, health, and slack.
WORKER_DB_POOL_HEADROOM = 4


def create_worker_engine(settings: WorkerSettings) -> Engine:
    """Ping on checkout, and size the pool for every thread that can hold a connection."""
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.finalize_workers + WORKER_DB_POOL_HEADROOM,
        max_overflow=0,
    )


def main() -> None:
    # uvicorn configures only its own loggers; without this the worker's INFO
    # events never reach compose logs at all.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = WorkerSettings()
    engine = create_worker_engine(settings)
    caps = DepCaps(settings)
    restaurant = RestaurantClient(settings, caps)
    courier = CourierClient(settings, caps)
    worker = Worker(settings, engine, caps=caps)
    kitchen = KitchenHandlers(settings, restaurant, now_fn=lambda: worker.now_fn())
    rides = CourierHandlers(settings, courier, now_fn=lambda: worker.now_fn())
    worker.register("confirm", kitchen.confirm)
    worker.register("poll_cook", kitchen.poll_cook)
    worker.register("void_ticket", kitchen.void_ticket)
    worker.register("dispatch", rides.dispatch)
    worker.register("poll_ride", rides.poll_ride)
    health_app = create_health_app(engine)

    async def runner() -> None:
        config = uvicorn.Config(
            health_app,
            host=settings.health_host,
            port=settings.health_port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        await asyncio.gather(server.serve(), worker.run())

    asyncio.run(runner())


if __name__ == "__main__":
    main()
