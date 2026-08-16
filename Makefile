.PHONY: check lint type-check tsc test

check: lint type-check tsc test

lint:
	uv run ruff check src tests alembic
	uv run ruff format --check src tests alembic

type-check:
	uv run mypy src tests alembic

tsc:
	npm --prefix dashboard ci
	npm --prefix dashboard run tsc

test:
	uv run pytest
