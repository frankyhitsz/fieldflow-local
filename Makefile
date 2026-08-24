.PHONY: setup lint audit test test-frontend test-e2e build verify demo demo-check benchmark-smoke clean-data

PYTHON := .venv/bin/python
PYTEST := .venv/bin/pytest
NPM_CACHE := /tmp/fieldflow-npm-cache
PORT ?= 8000

setup:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
	cd frontend && npm ci --cache $(NPM_CACHE)

lint:
	$(PYTHON) -m ruff check backend tests scripts
	$(PYTHON) -m ruff format --check backend tests scripts
	$(PYTHON) -m pyright
	PYTHONPATH=. FIELDFLOW_DB=/tmp/fieldflow-openapi.db $(PYTHON) scripts/check_openapi.py
	cd frontend && npm run typecheck

test:
	FIELDFLOW_DB=/tmp/fieldflow-tests.db $(PYTEST) --cov=backend --cov-report=term-missing --cov-fail-under=75 -q

audit:
	.venv/bin/pip-audit --cache-dir /tmp/fieldflow-pip-audit -r requirements.txt
	cd frontend && npm audit --cache $(NPM_CACHE) --omit=dev --audit-level=high

test-frontend:
	cd frontend && npm test

test-e2e:
	cd frontend && npm run test:e2e

build:
	cd frontend && npm run build

demo-check:
	PYTHONPATH=. FIELDFLOW_DB=/tmp/fieldflow-demo-check.db $(PYTHON) scripts/demo_check.py

benchmark-smoke:
	PYTHONPATH=. $(PYTHON) scripts/benchmark_smoke.py

verify: lint audit test test-frontend build demo-check benchmark-smoke test-e2e

demo: build
	@echo "FieldFlow Local → http://127.0.0.1:$(PORT)"
	$(PYTHON) -m uvicorn backend.main:app --host 127.0.0.1 --port $(PORT)

clean-data:
	@echo "Remove fieldflow.db manually if you want to reset local scenario history."
