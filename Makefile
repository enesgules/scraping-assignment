.PHONY: install format lint lint-fix typecheck test validate

install:
	uv sync

format:
	uv run ruff format .

lint:
	uv run ruff check .

lint-fix:
	uv run ruff check --fix .

typecheck:
	uv run --with pyright pyright .

test:
	uv run pytest tests/

validate: format lint typecheck test
