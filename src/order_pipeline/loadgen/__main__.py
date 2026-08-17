"""Console / module entrypoint: `loadgen` or `python -m order_pipeline.loadgen`."""

from __future__ import annotations


def main() -> None:
    import uvicorn

    from order_pipeline.loadgen.app import create_app
    from order_pipeline.loadgen.settings import LoadgenSettings

    settings = LoadgenSettings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
