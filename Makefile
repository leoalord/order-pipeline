.PHONY: check lint type-check test

check: lint type-check test

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

type-check:
	uv run mypy src tests

test:
	uv run pytest
