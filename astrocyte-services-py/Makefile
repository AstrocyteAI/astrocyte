# Run from this directory:  make <target>
# Or from repo root:        make -C astrocyte-services-py <target>

SHELL := /bin/bash

ASTRO_ROOT := $(abspath ..)

DOCKER_COMPOSE ?= docker compose
COMPOSE        := $(DOCKER_COMPOSE) -f docker-compose.yml
COMPOSE_RB     := $(DOCKER_COMPOSE) -f docker-compose.yml -f docker-compose.runbook.yml

.PHONY: help runbook up down logs ps build rebuild rebuild-runbook clean live health migrate \
	ci ci-checks ci-shell ci-compose ci-pytest ci-rest-tests smoke-rest

help:
	@echo "astrocyte-services-py"
	@echo ""
	@echo "  make runbook          Postgres → SQL migrations → REST + runbook config (./scripts/runbook-up.sh)"
	@echo "  make up               Quick stack: compose up --build -d (in-app bootstrap DDL; no HNSW SQL path)"
	@echo "  make down             Stop and remove project containers (same compose merge as runbook)"
	@echo "  make logs             Follow logs for all services"
	@echo "  make ps               docker compose ps"
	@echo "  make build            Build images (--no-cache)"
	@echo "  make rebuild          down + up (quick path)"
	@echo "  make rebuild-runbook  down + runbook (full migrate + runbook stack)"
	@echo "  make live             GET /live — process up only (no DB; good smoke test)"
	@echo "  make health           GET /health — includes vector store / DB check"
	@echo "  make migrate          Run astrocyte-pgvector/scripts/migrate.sh (needs Postgres on host port)"
	@echo "  make clean            down and remove volumes (wipes Postgres data)"
	@echo "  make ci-checks        CI: pytest (astrocyte-py + astrocyte-rest), bash -n runbook-up.sh, compose config"
	@echo "  make smoke-rest       curl retain/recall against running REST (see scripts/smoke-rest.sh)"
	@echo ""

runbook:
	@./scripts/runbook-up.sh

up:
	@$(COMPOSE) up --build -d

down:
	@$(COMPOSE_RB) down

logs:
	@$(COMPOSE_RB) logs -f

ps:
	@$(COMPOSE_RB) ps

build:
	@$(COMPOSE_RB) build --no-cache

rebuild: down up

rebuild-runbook: down runbook

clean:
	@$(COMPOSE_RB) down -v

live:
	@set -a; [[ -f .env ]] && source ./.env; set +a; \
	port="$${ASTROCYTES_HTTP_PUBLISH_PORT:-8080}"; \
	curl -fsS --max-time 5 "http://127.0.0.1:$${port}/live"; echo

health:
	@set -a; [[ -f .env ]] && source ./.env; set +a; \
	port="$${ASTROCYTES_HTTP_PUBLISH_PORT:-8080}"; \
	curl -fsS --max-time 25 "http://127.0.0.1:$${port}/health"; echo

migrate:
	@set -a; [[ -f .env ]] && source ./.env; set +a; \
	if [[ -n "$${MIGRATE_DATABASE_URL:-}" ]]; then export DATABASE_URL="$$MIGRATE_DATABASE_URL"; \
	else export DATABASE_URL="postgresql://$${POSTGRES_USER:-astrocyte}:$${POSTGRES_PASSWORD:-astrocyte}@127.0.0.1:$${POSTGRES_PUBLISH_PORT:-5433}/$${POSTGRES_DB:-astrocyte}"; fi; \
	./astrocyte-pgvector/scripts/migrate.sh

# CI: astrocyte-py tests + shellcheck-style + compose merge + REST unit tests (no Docker stack required).
ci-checks: ci-shell ci-compose ci-pytest ci-rest-tests

ci-shell:
	@bash -n scripts/runbook-up.sh

ci-compose:
	@$(COMPOSE_RB) config -q

ci-pytest:
	@cd "$(ASTRO_ROOT)/astrocyte-py" && uv sync --extra dev && uv run pytest

ci-rest-tests:
	@cd astrocyte-rest && uv sync --extra dev && uv run pytest

ci: ci-checks

# Manual: requires stack up (`make runbook` or `make up`) and ASTROCYTES_HTTP_PUBLISH_PORT if not 8080.
smoke-rest:
	@./scripts/smoke-rest.sh
