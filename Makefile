.PHONY: install format lint lint-fix typecheck validate

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

validate: format lint typecheck
