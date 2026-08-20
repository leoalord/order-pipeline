.PHONY: check lint type-check tsc test playwright-install

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

# Optional browser smoke only. The pytest suite skips it when the browser is
# absent, so this is not part of `check`.
playwright-install:
	uv run playwright install chromium
