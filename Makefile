# Developer commands. On Windows use Git-Bash/WSL (or run the underlying commands shown here).
PY ?= python
VENV ?= .venv
BIN := $(VENV)/bin
ifeq ($(OS),Windows_NT)
BIN := $(VENV)/Scripts
endif
COMPOSE ?= docker compose

.PHONY: help setup up down logs ps test test-unit test-integration test-bdd coverage lint format typecheck frontend-test frontend-build \
        seed seed-small simulate scenario load-test benchmark sql-evidence chaos security sast deps-scan image-scan zap clean \
        local-db local-up k8s-render tf-validate train

help:            ## list targets
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | sort

setup:           ## create .env, venv, install Python + frontend dependencies, train the ML model
	@test -f .env || cp .env.example .env
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q -U pip
	$(BIN)/pip install -q -e ".[dev]" pixeltable-pgserver python-hcl2 bandit pip-audit psutil
	cd frontend && npm ci
	$(BIN)/python -m cvi.ml.train

up:              ## build and start the whole stack (Kafka, Redis, Mongo, MinIO, Postgres, services, UI, Grafana...)
	@test -f .env || cp .env.example .env
	$(COMPOSE) up --build -d
	@echo "UI http://localhost:8080 | API docs http://localhost:8000/docs | Grafana http://localhost:3000 | Jaeger http://localhost:16686"

down:            ## stop the stack (keep data)
	$(COMPOSE) down

logs:            ## follow service logs
	$(COMPOSE) logs -f --tail=100 api stream-processor validator ingestion-edge

ps:
	$(COMPOSE) ps

# ----- tests ---------------------------------------------------------------------------------------------------------
test:            ## all tests (unit + integration + contract + acceptance); uses an embedded PostgreSQL if TEST_ADMIN_DATABASE_URL is unset
	$(BIN)/python -m pytest tests

test-unit:
	$(BIN)/python -m pytest tests/unit

test-integration:
	$(BIN)/python -m pytest tests/integration tests/contract

test-bdd:
	$(BIN)/python -m pytest tests/acceptance

coverage:        ## coverage report, fails below 80%
	$(BIN)/python -m pytest tests --cov=cvi --cov-report=term-missing:skip-covered --cov-report=xml --cov-fail-under=80

frontend-test:
	cd frontend && npm test

frontend-build:
	cd frontend && npm run build

lint:            ## ruff + mypy + TypeScript
	$(BIN)/ruff check src tests scripts benchmarks
	$(BIN)/mypy src/cvi
	cd frontend && npx tsc -b

format:
	$(BIN)/ruff check --fix src tests scripts benchmarks && $(BIN)/ruff format src tests scripts benchmarks

# ----- data & simulation -----------------------------------------------------------------------------------------------
seed:            ## (re)seed the 100,000-vehicle synthetic dataset into the running compose database
	$(COMPOSE) run --rm init python scripts/seed.py --vehicles 100000 --history-alerts 1000000 --force

seed-small:      ## quick 5,000-vehicle seed
	$(COMPOSE) run --rm init python scripts/seed.py --vehicles 5000 --history-alerts 20000 --force

simulate:        ## start the demo simulator against the compose stack (SIM_MODE=demo|small|load|100k)
	$(COMPOSE) --profile sim up -d simulator

scenario:        ## inject a deterministic scenario: make scenario S=engine_failure|dangerous_driver|ev_battery|fleet_idling|suspicious_movement
	$(COMPOSE) run --rm --no-deps simulator python services/simulator/simulator.py scenario $(S) --sink kafka

e2e:             ## end-to-end check against the running stack
	$(BIN)/python scripts/e2e.py --base http://localhost:8000

# ----- performance -----------------------------------------------------------------------------------------------------
load-test:       ## k6: ingest (needs k6) then API
	k6 run -e BASE=http://localhost:8001 -e KEY=$${INGEST_API_KEY:-dev-device-key-change-me} tests/load/k6_ingest.js
	k6 run -e BASE=http://localhost:8000 tests/load/k6_api.js

benchmark:       ## local in-process benchmarks -> benchmarks/results/*.json
	$(BIN)/python benchmarks/bench_simulator.py
	$(BIN)/python benchmarks/bench_pipeline.py --vehicles 20000 --events 60000
	$(BIN)/python benchmarks/bench_api.py --base http://localhost:8000

sql-evidence:    ## regenerate docs/performance/sql-before-after.md from real EXPLAIN ANALYZE runs
	$(BIN)/python scripts/sql_evidence.py

chaos:           ## kill services in the compose stack and verify recovery
	bash scripts/chaos_compose.sh

# ----- security --------------------------------------------------------------------------------------------------------
security: sast deps-scan   ## SAST + dependency scan (image scan and DAST run in CI / need Docker)

sast:            ## bandit (Python) + semgrep when available
	$(BIN)/bandit -q -r src -ll
	-$(BIN)/semgrep --config p/python --config p/owasp-top-ten --error src

deps-scan:
	$(BIN)/pip-audit --skip-editable
	cd frontend && npm audit --audit-level=high

image-scan:      ## Trivy on the built images (requires trivy + docker)
	trivy image --severity HIGH,CRITICAL --exit-code 1 cvi-python:local
	trivy image --severity HIGH,CRITICAL --exit-code 1 cvi-frontend:local

zap:             ## OWASP ZAP baseline scan of the running API (requires docker)
	docker run --rm --network host -v $$(pwd):/zap/wrk ghcr.io/zaproxy/zaproxy:stable zap-api-scan.py -t http://localhost:8000/openapi.json -f openapi -r zap-report.html -I

# ----- infra -----------------------------------------------------------------------------------------------------------
k8s-render:      ## render and sanity-check Kubernetes manifests
	kubectl kustomize infrastructure/kubernetes/overlays/dev >/dev/null && kubectl kustomize infrastructure/kubernetes/overlays/aws >/dev/null && echo ok

tf-validate:
	cd infrastructure/terraform/aws && terraform init -backend=false && terraform validate

train:           ## train + evaluate the predictive-maintenance model (writes ml/artifacts)
	$(BIN)/python -m cvi.ml.train

# ----- Docker-free local mode (embedded PostgreSQL + in-process broker; see README) ---------------------------------------
local-db:        ## start an embedded PostgreSQL (pixeltable-pgserver) and write .data/dev.env
	$(BIN)/python scripts/dev_postgres.py

local-up:        ## API + validator + stream processor in ONE process (needs `make local-db` and a seeded DB)
	$(BIN)/python -m cvi.local

clean:           ## remove containers, volumes, build artifacts and local data
	-$(COMPOSE) down -v --remove-orphans
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov coverage.xml frontend/dist .data ml/artifacts benchmarks/results
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
