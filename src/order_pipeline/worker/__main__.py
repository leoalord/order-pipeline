"""Console / module entrypoint: `worker` or `python -m order_pipeline.worker`."""

from __future__ import annotations

import asyncio

import uvicorn
from sqlalchemy import create_engine

from order_pipeline.worker.chassis import Worker
from order_pipeline.worker.deps import DepCaps
from order_pipeline.worker.health import create_health_app
from order_pipeline.worker.http import RestaurantClient
from order_pipeline.worker.kitchen import KitchenHandlers
from order_pipeline.worker.settings import WorkerSettings


def main() -> None:
    settings = WorkerSettings()
    engine = create_engine(settings.database_url)
    caps = DepCaps(settings)
    restaurant = RestaurantClient(settings, caps)
    worker = Worker(settings, engine, caps=caps)
    kitchen = KitchenHandlers(settings, restaurant, now_fn=lambda: worker.now_fn())
    worker.register("confirm", kitchen.confirm)
    worker.register("poll_cook", kitchen.poll_cook)
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
