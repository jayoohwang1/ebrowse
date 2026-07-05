.PHONY: test test-browser test-all lint fmt setup

setup:
	uv sync --extra dev
	uv run playwright install chromium

test:
	uv run pytest -m "not browser and not e2e and not live" -q

test-browser:
	uv run pytest -m "not live" -q

test-all:
	uv run pytest -q

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests
