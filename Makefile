.PHONY: install test lint format type smoke regen build clean

install:
	uv venv && uv pip install -e ".[dev]"

test:
	pytest -q

lint:
	ruff check . && ruff format --check .

format:
	ruff format .

type:
	mypy src/lenz_io

# Smoke tests against staging. Requires:
#   export LENZ_E2E_KEY=lenz_...
#   (optional) export LENZ_BASE_URL=https://staging.lenz.io/api/v1
smoke:
	pytest -m smoke -q

# Refresh the openapi.json snapshot from the main Lenz repo and re-run
# any generator-based steps. Today this is a manual operation — the
# checked-in openapi.json is the source of truth.
#
# Usage: LENZ_REPO=/path/to/Lenz make regen
LENZ_REPO ?= ../Lenz
regen:
	@if [ ! -d "$(LENZ_REPO)" ]; then \
		echo "Set LENZ_REPO=/path/to/Lenz (currently '$(LENZ_REPO)')"; exit 1; \
	fi
	cd $(LENZ_REPO) && uv run python manage.py emit_openapi --output $(CURDIR)/openapi.json --pretty
	@echo "Refreshed openapi.json from $(LENZ_REPO)"
	@echo "Inspect with: git diff openapi.json"

build:
	python -m build

clean:
	rm -rf build/ dist/ .pytest_cache .mypy_cache .ruff_cache *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
