.PHONY: setup lock-check lint audit test mutation-test mutation-smoke test-frontend test-e2e build verify demo demo-check benchmark benchmark-smoke clean-data

PYTHON := .venv/bin/python
PYTEST := .venv/bin/pytest
NPM_CACHE := /tmp/fieldflow-npm-cache
PORT ?= 8000

setup:
	python3 -m venv .venv
	.venv/bin/pip install --require-hashes -r requirements-dev.lock
	.venv/bin/pip install -e . --no-deps
	cd frontend && npm ci --cache $(NPM_CACHE)

lock-check:
	$(PYTHON) scripts/check_lockfiles.py
	$(PYTHON) -m pip install --dry-run --require-hashes -r requirements-runtime.lock

lint:
	$(PYTHON) -m ruff check backend tests scripts
	$(PYTHON) -m ruff format --check backend tests scripts
	$(PYTHON) -m pyright
	PYTHONPATH=. FIELDFLOW_DB=/tmp/fieldflow-openapi.db $(PYTHON) scripts/check_openapi.py
	cd frontend && npm run check:api && npm run lint && npm run typecheck

test:
	FIELDFLOW_DB=/tmp/fieldflow-tests.db $(PYTEST) --cov=backend --cov-report=term-missing --cov-fail-under=85 -q

mutation-test:
	$(PYTHON) scripts/mutation_smoke.py

mutation-smoke: mutation-test

audit:
	.venv/bin/pip-audit --cache-dir /tmp/fieldflow-pip-audit -r requirements-runtime.lock
	.venv/bin/pip-audit --cache-dir /tmp/fieldflow-pip-audit -r requirements-dev.lock
	cd frontend && npm audit --cache $(NPM_CACHE) --omit=dev --audit-level=high

test-frontend:
	cd frontend && npm test

test-e2e:
	cd frontend && npm run test:e2e

build:
	cd frontend && npm run build

demo-check:
	PYTHONPATH=. $(PYTHON) scripts/demo_check.py

benchmark:
	PYTHONPATH=. $(PYTHON) scripts/benchmark_smoke.py

benchmark-smoke: benchmark

verify: lock-check lint audit test mutation-test test-frontend build demo-check benchmark test-e2e

demo: build
	@echo "FieldFlow Local → http://127.0.0.1:$(PORT)"
	$(PYTHON) -m uvicorn backend.main:app --host 127.0.0.1 --port $(PORT)

clean-data:
	@echo "Remove fieldflow.db manually if you want to reset local scenario history."
