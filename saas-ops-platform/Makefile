.DEFAULT_GOAL := help
COMPOSE := docker compose
KEY = $(shell grep '^OPS_API_KEY=' .env 2>/dev/null | cut -d= -f2-)
OPS := http://localhost:8080

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

init: ## Create .env with random secrets (once)
	@./scripts/init-env.sh

up: init ## Build and start the whole stack
	$(COMPOSE) up -d --build --wait
	@echo "\nDashboard  $(OPS)\nGrafana    http://localhost:3000\nPrometheus http://localhost:9090\nAlerts     http://localhost:9093"

down: ## Stop the stack (keeps data)
	$(COMPOSE) down

clean: ## Stop the stack and delete all data volumes
	$(COMPOSE) down -v

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Follow ops platform logs
	$(COMPOSE) logs -f ops-platform

dev: ## Run everything locally without Docker
	./scripts/dev.sh

install: ## Install Python dev dependencies
	pip install -r ops-platform/requirements-dev.txt -r services/sim/requirements.txt

test: ## Run unit and integration tests
	python -m pytest -q

lint: ## Lint and check formatting
	ruff check . && ruff format --check .

fmt: ## Auto-format
	ruff check --fix . && ruff format .

dashboard: ## Regenerate the Grafana dashboard JSON
	python grafana/build_dashboard.py

smoke: ## End-to-end test against the running stack (crashes things on purpose)
	./scripts/smoke-test.sh

crash-api: ## Chaos: crash the API and watch it come back
	curl -fsS -X POST -H "X-API-Key: $(KEY)" $(OPS)/api/chaos/saas-api/crash; echo

kill-db: ## Chaos: kill the database container
	curl -fsS -X POST -H "X-API-Key: $(KEY)" $(OPS)/api/chaos/saas-db/kill; echo

report: ## Build today's report so far
	curl -fsS -X POST -H "X-API-Key: $(KEY)" $(OPS)/api/reports/generate | python -m json.tool | head -40

.PHONY: help init up down clean ps logs dev install test lint fmt dashboard smoke crash-api kill-db report
