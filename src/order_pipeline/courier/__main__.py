"""Console / module entrypoint: `courier-sim` or `python -m order_pipeline.courier`."""

from __future__ import annotations


def main() -> None:
    import uvicorn

    from order_pipeline.courier.settings import CSIMSettings

    settings = CSIMSettings()
    uvicorn.run(
        "order_pipeline.courier.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
