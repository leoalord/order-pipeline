.PHONY: check lint type-check test

check: lint type-check test

lint:
	uv run ruff check src tests alembic
	uv run ruff format --check src tests alembic

type-check:
	uv run mypy src tests alembic

test:
	uv run pytest
