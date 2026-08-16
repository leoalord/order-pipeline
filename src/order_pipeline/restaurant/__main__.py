"""Console / module entrypoint: `restaurant-sim` or `python -m order_pipeline.restaurant`."""

from __future__ import annotations


def main() -> None:
    import uvicorn

    from order_pipeline.restaurant.settings import RSIMSettings

    settings = RSIMSettings()
    uvicorn.run(
        "order_pipeline.restaurant.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
