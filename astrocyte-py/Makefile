SHELL := /bin/bash

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
DATASETS_DIR  := datasets
LOCOMO_DIR    := $(DATASETS_DIR)/locomo
LONGMEMEVAL_DIR := $(DATASETS_DIR)/longmemeval
RESULTS_DIR   := benchmark-results

# ---------------------------------------------------------------------------
# Benchmark database (postgres-age-pgvector)
# ---------------------------------------------------------------------------
BENCH_PG_CONTAINER := astrocyte-bench-pg
BENCH_PG_PORT      := 5433
BENCH_PG_IMAGE     := ghcr.io/astrocyteai/astrocyte/postgres-age-pgvector:latest
BENCH_PG_LOCAL_IMG := astrocyte/postgres-age-pgvector:local
BENCH_PG_USER      := astrocyte
BENCH_PG_PASSWORD  := astrocyte
BENCH_PG_DB        := astrocyte_bench
BENCH_EMBEDDING_DIMENSIONS := 1536
BENCH_DATABASE_URL := postgresql://$(BENCH_PG_USER):$(BENCH_PG_PASSWORD)@127.0.0.1:$(BENCH_PG_PORT)/$(BENCH_PG_DB)
BENCH_UV_RUN := uv run --extra dev

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

.PHONY: test
test:
	uv sync --extra dev
	uv run python -m pytest

# ---------------------------------------------------------------------------
# Dataset fetching (one-time, gitignored)
# ---------------------------------------------------------------------------

.PHONY: fetch-locomo fetch-longmemeval fetch-datasets

fetch-locomo: $(LOCOMO_DIR)/data/locomo10.json
$(LOCOMO_DIR)/data/locomo10.json:
	@echo "Fetching LoCoMo dataset..."
	@mkdir -p $(DATASETS_DIR)
	git clone --depth 1 https://github.com/snap-research/locomo.git $(LOCOMO_DIR)
	@echo "LoCoMo dataset ready at $(LOCOMO_DIR)/data/"

fetch-longmemeval: $(LONGMEMEVAL_DIR)/data/longmemeval_s_cleaned.json
$(LONGMEMEVAL_DIR)/data/longmemeval_s_cleaned.json:
	@echo "Fetching LongMemEval dataset..."
	@mkdir -p $(DATASETS_DIR)
	git clone --depth 1 https://github.com/xiaowu0162/LongMemEval.git $(LONGMEMEVAL_DIR)
	@mkdir -p $(LONGMEMEVAL_DIR)/data
	wget -q -P $(LONGMEMEVAL_DIR)/data/ \
		https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
	@echo "LongMemEval dataset ready at $(LONGMEMEVAL_DIR)/data/"

fetch-datasets: fetch-locomo fetch-longmemeval

# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------

.PHONY: bench-db-start bench-db-stop bench-db-reset bench bench-full bench-gate bench-locomo bench-locomo-fair bench-locomo-quick bench-locomo-litellm bench-locomo-litellm-quick bench-compare bench-longmemeval bench-longmemeval-quick bench-builtin bench-smoke

# Start the benchmark Postgres container (postgres-age-pgvector).
# Pulls from GHCR first; falls back to building locally from the Dockerfile.
# No-ops if the container is already running. Restarts if stopped.
bench-db-start:
	@set -e; \
	if docker ps --format '{{.Names}}' | grep -q '^$(BENCH_PG_CONTAINER)$$'; then \
		echo "  [bench-db] $(BENCH_PG_CONTAINER) already running"; \
	elif docker ps -a --format '{{.Names}}' | grep -q '^$(BENCH_PG_CONTAINER)$$'; then \
		echo "  [bench-db] Restarting stopped container $(BENCH_PG_CONTAINER)"; \
		docker start $(BENCH_PG_CONTAINER); \
		until docker exec $(BENCH_PG_CONTAINER) pg_isready -U $(BENCH_PG_USER) -d $(BENCH_PG_DB) -q 2>/dev/null; do sleep 1; done; \
		until PGPASSWORD=$(BENCH_PG_PASSWORD) psql "$(BENCH_DATABASE_URL)" -Atc "SELECT 1" >/dev/null 2>&1; do sleep 1; done; \
		echo "  [bench-db] Postgres ready"; \
	else \
		echo "  [bench-db] Pulling $(BENCH_PG_IMAGE)..."; \
		if docker pull $(BENCH_PG_IMAGE); then \
			BENCH_IMG=$(BENCH_PG_IMAGE); \
		else \
			echo "  [bench-db] Pull failed — building from ../docker/postgres-age-pgvector/Dockerfile"; \
			docker build -t $(BENCH_PG_LOCAL_IMG) -f ../docker/postgres-age-pgvector/Dockerfile ..; \
			BENCH_IMG=$(BENCH_PG_LOCAL_IMG); \
		fi; \
		docker run -d --name $(BENCH_PG_CONTAINER) \
			-e POSTGRES_USER=$(BENCH_PG_USER) \
			-e POSTGRES_PASSWORD=$(BENCH_PG_PASSWORD) \
			-e POSTGRES_DB=$(BENCH_PG_DB) \
			-p $(BENCH_PG_PORT):5432 \
			$$BENCH_IMG; \
		echo "  [bench-db] Waiting for Postgres to be ready..."; \
		until docker exec $(BENCH_PG_CONTAINER) pg_isready -U $(BENCH_PG_USER) -d $(BENCH_PG_DB) -q 2>/dev/null; do sleep 1; done; \
		until PGPASSWORD=$(BENCH_PG_PASSWORD) psql "$(BENCH_DATABASE_URL)" -Atc "SELECT 1" >/dev/null 2>&1; do sleep 1; done; \
		echo "  [bench-db] Postgres ready at localhost:$(BENCH_PG_PORT)"; \
	fi; \
	echo "  [bench-db] Applying pgvector HNSW migrations (vector($(BENCH_EMBEDDING_DIMENSIONS)))"; \
	ASTROCYTE_EMBEDDING_DIMENSIONS=$(BENCH_EMBEDDING_DIMENSIONS) \
		DATABASE_URL="$(BENCH_DATABASE_URL)" \
		../adapters-storage-py/astrocyte-pgvector/scripts/migrate.sh; \
	actual=$$(PGPASSWORD=$(BENCH_PG_PASSWORD) psql "$(BENCH_DATABASE_URL)" -Atc "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a WHERE a.attrelid = 'astrocyte_vectors'::regclass AND a.attname = 'embedding'"); \
	if [ "$$actual" != "vector($(BENCH_EMBEDDING_DIMENSIONS))" ]; then \
		echo "  [bench-db] ERROR: astrocyte_vectors.embedding is $$actual, expected vector($(BENCH_EMBEDDING_DIMENSIONS))."; \
		echo "  [bench-db] Run 'make bench-db-reset' once to recreate the disposable benchmark database."; \
		exit 1; \
	fi

# Stop and remove the benchmark Postgres container.
bench-db-stop:
	@docker stop $(BENCH_PG_CONTAINER) 2>/dev/null && docker rm $(BENCH_PG_CONTAINER) 2>/dev/null && echo "  [bench-db] $(BENCH_PG_CONTAINER) stopped" || echo "  [bench-db] $(BENCH_PG_CONTAINER) not running"

# Recreate the disposable benchmark database, useful after embedding dimension changes.
bench-db-reset: bench-db-stop bench-db-start

# Full LoCoMo benchmark (requires OPENAI_API_KEY, ~30-60 min)
# Pass RESUME=1 to continue an interrupted run: make bench-locomo RESUME=1
bench-locomo: fetch-locomo bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--canonical-judge \
		$(if $(RESUME),--resume,)

# Quick LoCoMo subset (50 questions, ~2-3 min)
bench-locomo-quick: fetch-locomo bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--max-questions 50 \
		--canonical-judge

# Fair-coverage fast LoCoMo: 20 questions × 10 conversations = 200 total
# with EVERY conversation represented. Recommended fast-iteration sample
# — replaces the prior bench-locomo-200 (head-slice) which over-weighted
# early conversations and obscured per-category signal.
# ~15-20 min wall time, ~$1 cost.
bench-locomo-fair: fetch-locomo bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--max-questions-per-conversation 20 \
		--canonical-judge

# Full LongMemEval benchmark (requires OPENAI_API_KEY, ~2-4 hrs for all 500 questions)
# Pass RESUME=1 to continue an interrupted run: make bench-longmemeval RESUME=1
bench-longmemeval: fetch-longmemeval bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks longmemeval \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--canonical-judge \
		$(if $(RESUME),--resume,)

# Quick LongMemEval subset (100 questions, stratified across all categories, ~30-60 min)
bench-longmemeval-quick: fetch-longmemeval bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks longmemeval \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--max-questions 100 \
		--canonical-judge

# LoCoMo via LiteLLM adapter (requires OPENAI_API_KEY + astrocyte-llm-litellm installed)
bench-locomo-litellm: fetch-locomo bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config benchmarks/config-litellm.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data

# Quick LoCoMo via LiteLLM (50 questions)
bench-locomo-litellm-quick: fetch-locomo bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config benchmarks/config-litellm.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--max-questions 50

# Side-by-side: built-in OpenAI vs LiteLLM adapter (50 questions each)
bench-compare: fetch-locomo bench-db-start
	@echo "=== Built-in OpenAI provider ==="
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--max-questions 50 \
		--output-dir $(RESULTS_DIR)/openai
	@echo ""
	@echo "=== LiteLLM adapter ==="
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config benchmarks/config-litellm.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--max-questions 50 \
		--output-dir $(RESULTS_DIR)/litellm
	@echo ""
	@echo "Results: $(RESULTS_DIR)/openai/latest.json vs $(RESULTS_DIR)/litellm/latest.json"

# Built-in eval suites (basic + accuracy)
bench-builtin: bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks builtin

# All benchmarks
bench: fetch-datasets bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks builtin longmemeval locomo \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--locomo-path $(LOCOMO_DIR)/data

# Full canonical run — LME + LoCoMo in parallel, LLM-judge, competitor-comparable numbers.
# Requires API key (use: doppler run -- make bench-full)
# Pass RESUME=1 to continue an interrupted run: doppler run -- make bench-full RESUME=1
bench-full: fetch-datasets bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks longmemeval locomo \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--locomo-path $(LOCOMO_DIR)/data \
		--canonical-judge \
		$(if $(RESUME),--resume,)

# Release gate for Hindsight-informed capability claims.
# Expects benchmark-results/latest.json from run_benchmarks.py.
bench-gate:
	uv run python scripts/check_benchmark_gates.py \
		--gates benchmarks/gates-hindsight-informed.json \
		--results $(RESULTS_DIR)/latest.json

# Smoke test — in-memory providers, no API key needed.
# Uses the real LoCoMo dataset (all 200q) so the regression gate in CI
# covers all five categories. With the mock provider this runs in ~25s.
bench-smoke: fetch-locomo
	uv run python scripts/run_benchmarks.py \
		--provider test \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data

# ---------------------------------------------------------------------------
# CodeQL
# ---------------------------------------------------------------------------

CODEQL_DB      := /tmp/astrocyte-codeql-db
CODEQL_OUT     := /tmp/astrocyte-findings.csv
CODEQL_QUERIES := \
	~/.codeql/packages/codeql/python-queries/1.8.0/Statements/StatementNoEffect.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Statements/UnreachableCode.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Statements/AssertOnTuple.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Statements/RedundantAssignment.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Functions/ConsistentReturns.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Functions/ModificationOfParameterWithDefault.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Imports/ImportandImportFrom.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Imports/UnusedImport.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Exceptions/EmptyExcept.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Exceptions/IncorrectExceptOrder.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Variables/UninitializedLocal.ql

.PHONY: codeql
codeql:
	codeql database create $(CODEQL_DB) --language=python --source-root . --overwrite
	codeql database analyze $(CODEQL_DB) $(CODEQL_QUERIES) --format=csv --output=$(CODEQL_OUT)
	@echo "--- Findings (excluding datasets/) ---"
	@python3 scripts/filter_codeql.py $(CODEQL_OUT)

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

.PHONY: clean-datasets clean-results

clean-datasets:
	rm -rf $(DATASETS_DIR)

clean-results:
	rm -rf $(RESULTS_DIR)
