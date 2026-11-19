.PHONY: install test lint fix run

install:
	python -m pip install -e ".[dev]"

test:
	python -m pytest tests/ -q

lint:
	python -m ruff check src/ tests/

fix:
	python -m ruff check --fix src/ tests/

run:
	python -m live_relay.main
