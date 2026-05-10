SHELL := /bin/bash

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
DATASETS_DIR  := datasets
LOCOMO_DIR    := $(DATASETS_DIR)/locomo
LONGMEMEVAL_DIR := $(DATASETS_DIR)/longmemeval
RESULTS_DIR   := benchmark-results
CONFIG        ?= benchmarks/config.yaml

# ---------------------------------------------------------------------------
# Benchmark database (astrocyte-postgres)
# ---------------------------------------------------------------------------
BENCH_PG_CONTAINER := astrocyte-bench-pg
BENCH_PG_PORT      := 5433
# Default to the LOCAL image because the upstream
# ghcr.io/astrocyteai/astrocyte/astrocyte-postgres:latest predates the
# 2026-05-06 default switch to pgvectorscale and doesn't have the
# vectorscale or vchord binaries baked in. The bench-db-start target
# will fall through to building from docker/astrocyte-postgres/Dockerfile
# if the local image is missing. Override with
# ``make bench-db-start BENCH_PG_IMAGE=ghcr.io/...`` once an upstream
# image with all three backends is published.
BENCH_PG_IMAGE     := astrocyte/astrocyte-postgres:local
BENCH_PG_LOCAL_IMG := astrocyte/astrocyte-postgres:local
BENCH_PG_USER      := astrocyte
BENCH_PG_PASSWORD  := astrocyte
BENCH_PG_DB        := astrocyte_bench
BENCH_EMBEDDING_DIMENSIONS := 1536
BENCH_DATABASE_URL := postgresql://$(BENCH_PG_USER):$(BENCH_PG_PASSWORD)@127.0.0.1:$(BENCH_PG_PORT)/$(BENCH_PG_DB)
BENCH_UV_RUN := uv run --extra dev --extra rerank
# ANN backend for the bench Postgres images. Drives the migration's
# CREATE EXTENSION + USING clause for vector indexes.
#   pgvectorscale  (default)  — DiskANN indexes. Better concurrent-insert
#                                throughput than HNSW; chosen as default
#                                after the 2026-05 LME bench observed
#                                HNSW per-page write-lock drift
#                                (1.0s → 2.0s/session as the index
#                                grew). pgvectorscale is OSS under the
#                                PostgreSQL License.
#   pgvector                  — HNSW indexes. Use when the pgvectorscale
#                                binary is unavailable.
#   vchord                    — VectorChord vchordrq. Vendor-cited
#                                highest insert throughput; opt-in.
#
# Override per-run: ``make bench-db-reset BENCH_VECTOR_EXTENSION=pgvector``
BENCH_VECTOR_EXTENSION ?= pgvectorscale

# ---------------------------------------------------------------------------
# Second benchmark database — for running LoCoMo + LME in parallel without
# state-reset interference. Each `run_benchmarks.py` invocation TRUNCATEs all
# bench tables on startup; sharing one DB between two processes makes the
# second TRUNCATE wipe the first run's in-flight retain. Two containers on
# different ports give each run an isolated PgQueuer queue, observation
# table, mental_model table, and entity graph.
#
# Run pattern:
#   Terminal A: doppler run -- make bench-locomo            (DB on :5433)
#   Terminal B: doppler run -- make bench-longmemeval-on-2  (DB on :5434)
BENCH_PG_2_CONTAINER := astrocyte-bench-pg-2
BENCH_PG_2_PORT      := 5434
BENCH_DATABASE_URL_2 := postgresql://$(BENCH_PG_USER):$(BENCH_PG_PASSWORD)@127.0.0.1:$(BENCH_PG_2_PORT)/$(BENCH_PG_DB)

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

.PHONY: bench-db-start bench-db-stop bench-db-reset bench-db-start-2 bench-db-stop-2 bench-db-reset-2 bench bench-full bench-gate bench-locomo bench-locomo-fair bench-locomo-quick bench-locomo-litellm bench-locomo-litellm-quick bench-compare bench-longmemeval bench-longmemeval-quick bench-longmemeval-on-2 bench-longmemeval-quick-on-2 bench-builtin bench-smoke

# Start the benchmark Postgres container (astrocyte-postgres).
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
			echo "  [bench-db] Pull failed — building from ../docker/astrocyte-postgres/Dockerfile"; \
			docker build -t $(BENCH_PG_LOCAL_IMG) -f ../docker/astrocyte-postgres/Dockerfile ..; \
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
	echo "  [bench-db] Applying $(BENCH_VECTOR_EXTENSION) migrations (vector($(BENCH_EMBEDDING_DIMENSIONS)))"; \
	ASTROCYTE_EMBEDDING_DIMENSIONS=$(BENCH_EMBEDDING_DIMENSIONS) \
		VECTOR_EXTENSION=$(BENCH_VECTOR_EXTENSION) \
		DATABASE_URL="$(BENCH_DATABASE_URL)" \
		../adapters-storage-py/astrocyte-postgres/scripts/migrate.sh; \
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

# ---------------------------------------------------------------------------
# Second benchmark database (for parallel LoCoMo + LME runs)
# ---------------------------------------------------------------------------
# Mirrors bench-db-start/stop/reset but with the second container/port. Body
# is intentionally duplicated rather than abstracted into a make-macro: this
# is an ops file and direct grep-ability beats DRY.

bench-db-start-2:
	@set -e; \
	if docker ps --format '{{.Names}}' | grep -q '^$(BENCH_PG_2_CONTAINER)$$'; then \
		echo "  [bench-db-2] $(BENCH_PG_2_CONTAINER) already running"; \
	elif docker ps -a --format '{{.Names}}' | grep -q '^$(BENCH_PG_2_CONTAINER)$$'; then \
		echo "  [bench-db-2] Restarting stopped container $(BENCH_PG_2_CONTAINER)"; \
		docker start $(BENCH_PG_2_CONTAINER); \
		until docker exec $(BENCH_PG_2_CONTAINER) pg_isready -U $(BENCH_PG_USER) -d $(BENCH_PG_DB) -q 2>/dev/null; do sleep 1; done; \
		until PGPASSWORD=$(BENCH_PG_PASSWORD) psql "$(BENCH_DATABASE_URL_2)" -Atc "SELECT 1" >/dev/null 2>&1; do sleep 1; done; \
		echo "  [bench-db-2] Postgres ready"; \
	else \
		echo "  [bench-db-2] Pulling $(BENCH_PG_IMAGE)..."; \
		if docker pull $(BENCH_PG_IMAGE); then \
			BENCH_IMG=$(BENCH_PG_IMAGE); \
		else \
			echo "  [bench-db-2] Pull failed — building from ../docker/astrocyte-postgres/Dockerfile"; \
			docker build -t $(BENCH_PG_LOCAL_IMG) -f ../docker/astrocyte-postgres/Dockerfile ..; \
			BENCH_IMG=$(BENCH_PG_LOCAL_IMG); \
		fi; \
		docker run -d --name $(BENCH_PG_2_CONTAINER) \
			-e POSTGRES_USER=$(BENCH_PG_USER) \
			-e POSTGRES_PASSWORD=$(BENCH_PG_PASSWORD) \
			-e POSTGRES_DB=$(BENCH_PG_DB) \
			-p $(BENCH_PG_2_PORT):5432 \
			$$BENCH_IMG; \
		echo "  [bench-db-2] Waiting for Postgres to be ready..."; \
		until docker exec $(BENCH_PG_2_CONTAINER) pg_isready -U $(BENCH_PG_USER) -d $(BENCH_PG_DB) -q 2>/dev/null; do sleep 1; done; \
		until PGPASSWORD=$(BENCH_PG_PASSWORD) psql "$(BENCH_DATABASE_URL_2)" -Atc "SELECT 1" >/dev/null 2>&1; do sleep 1; done; \
		echo "  [bench-db-2] Postgres ready at localhost:$(BENCH_PG_2_PORT)"; \
	fi; \
	echo "  [bench-db-2] Applying $(BENCH_VECTOR_EXTENSION) migrations (vector($(BENCH_EMBEDDING_DIMENSIONS)))"; \
	ASTROCYTE_EMBEDDING_DIMENSIONS=$(BENCH_EMBEDDING_DIMENSIONS) \
		VECTOR_EXTENSION=$(BENCH_VECTOR_EXTENSION) \
		DATABASE_URL="$(BENCH_DATABASE_URL_2)" \
		../adapters-storage-py/astrocyte-postgres/scripts/migrate.sh; \
	actual=$$(PGPASSWORD=$(BENCH_PG_PASSWORD) psql "$(BENCH_DATABASE_URL_2)" -Atc "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a WHERE a.attrelid = 'astrocyte_vectors'::regclass AND a.attname = 'embedding'"); \
	if [ "$$actual" != "vector($(BENCH_EMBEDDING_DIMENSIONS))" ]; then \
		echo "  [bench-db-2] ERROR: astrocyte_vectors.embedding is $$actual, expected vector($(BENCH_EMBEDDING_DIMENSIONS))."; \
		echo "  [bench-db-2] Run 'make bench-db-reset-2' once to recreate the disposable benchmark database."; \
		exit 1; \
	fi

bench-db-stop-2:
	@docker stop $(BENCH_PG_2_CONTAINER) 2>/dev/null && docker rm $(BENCH_PG_2_CONTAINER) 2>/dev/null && echo "  [bench-db-2] $(BENCH_PG_2_CONTAINER) stopped" || echo "  [bench-db-2] $(BENCH_PG_2_CONTAINER) not running"

bench-db-reset-2: bench-db-stop-2 bench-db-start-2

# Full LoCoMo benchmark (requires OPENAI_API_KEY, ~30-60 min)
# Pass RESUME=1 to continue an interrupted run: make bench-locomo RESUME=1
bench-locomo: fetch-locomo bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config $(CONFIG) \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--canonical-judge \
		$(if $(MULTI_QUERY),--multi-query,) \
		$(if $(RESUME),--resume,)

# Quick LoCoMo subset (50 questions, ~2-3 min)
bench-locomo-quick: fetch-locomo bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config $(CONFIG) \
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
		--config $(CONFIG) \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--max-questions-per-conversation 20 \
		--canonical-judge \
		$(if $(RETAIN_CONCURRENCY),--retain-concurrency $(RETAIN_CONCURRENCY),) \
		$(if $(RESUME),--resume,)

# Full LongMemEval benchmark (requires OPENAI_API_KEY, ~2-4 hrs for all 500 questions)
# Pass RESUME=1 to continue an interrupted run: make bench-longmemeval RESUME=1
bench-longmemeval: fetch-longmemeval bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config $(CONFIG) \
		--benchmarks longmemeval \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--canonical-judge \
		$(if $(RESUME),--resume,)

# Quick LongMemEval subset (100 questions, stratified across all categories, ~30-60 min)
bench-longmemeval-quick: fetch-longmemeval bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config $(CONFIG) \
		--benchmarks longmemeval \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--max-questions 100 \
		--canonical-judge

# LongMemEval pointed at the SECOND DB — for parallel runs alongside `make bench-locomo`.
# Output dir is separate so result files don't collide.
# Pass RESUME=1 to continue an interrupted run.
bench-longmemeval-on-2: fetch-longmemeval bench-db-start-2
	DATABASE_URL="$(BENCH_DATABASE_URL_2)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL_2)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config $(CONFIG) \
		--benchmarks longmemeval \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--canonical-judge \
		--output-dir $(RESULTS_DIR)/lme-on-2 \
		$(if $(MULTI_QUERY),--multi-query,) \
		$(if $(RESUME),--resume,)

# Quick LongMemEval subset on the SECOND DB (100q, ~30-60 min) — for fast smoke tests
# while a LoCoMo run is in flight on the primary DB.
bench-longmemeval-quick-on-2: fetch-longmemeval bench-db-start-2
	DATABASE_URL="$(BENCH_DATABASE_URL_2)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL_2)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config $(CONFIG) \
		--benchmarks longmemeval \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--max-questions 100 \
		--canonical-judge \
		--output-dir $(RESULTS_DIR)/lme-on-2 \
		$(if $(MULTI_QUERY),--multi-query,)

# ---------------------------------------------------------------------------
# Single-command parallel run with full process-level isolation.
# ---------------------------------------------------------------------------
# Backgrounds bench-locomo (DB :5433) and bench-longmemeval-on-2 (DB :5434)
# under one make target.  Each gets its own Postgres container, python
# process, and log file — one crash does not affect the other.  ``wait``
# blocks the parent shell until BOTH children finish, so make returns
# only when both runs complete.
#
# Live progress: in another terminal,
#     tail -f $(RESULTS_DIR)/parallel/locomo.log
#     tail -f $(RESULTS_DIR)/parallel/lme.log
#
# Usage:
#     doppler run -- make bench-parallel-isolated
#     doppler run -- make bench-parallel-isolated CONFIG=benchmarks/config-foo.yaml
#     doppler run -- make bench-parallel-isolated RESUME=1
#
# Per-leg config override (LoCoMo and LME need different
# adversarial_defense settings — LoCoMo's adversarial bucket scores
# by abstaining, LME has no such bucket):
#     doppler run -- make bench-parallel-isolated \
#         LOCOMO_CONFIG=benchmarks/config-X-locomo.yaml \
#         LME_CONFIG=benchmarks/config-X-lme.yaml
#
# CONFIG, LOCOMO_CONFIG, LME_CONFIG precedence: per-leg overrides
# CONFIG. When neither is set, falls back to the bench-locomo /
# bench-longmemeval-on-2 defaults.
#
# Requires fresh-or-resumed state on each DB.  Use ``make bench-db-reset``
# and ``make bench-db-reset-2`` first when starting a new experiment.
.PHONY: bench-parallel-isolated
bench-parallel-isolated: fetch-datasets bench-db-start bench-db-start-2
	@mkdir -p $(RESULTS_DIR)/parallel
	@echo "  [parallel] LoCoMo on :5433 + LME on :5434"
	@echo "  [parallel] LoCoMo config: $(or $(LOCOMO_CONFIG),$(CONFIG))"
	@echo "  [parallel]    LME config: $(or $(LME_CONFIG),$(CONFIG))"
	@echo "  [parallel] Logs: $(RESULTS_DIR)/parallel/{locomo,lme}.log"
	@echo "  [parallel] Watch live: tail -f $(RESULTS_DIR)/parallel/locomo.log"
	@( $(MAKE) bench-locomo CONFIG=$(or $(LOCOMO_CONFIG),$(CONFIG)) $(if $(MULTI_QUERY),MULTI_QUERY=1,) $(if $(RESUME),RESUME=1,) > $(RESULTS_DIR)/parallel/locomo.log 2>&1 ; \
	   echo "  [parallel] locomo exited ($$?)" ) & \
	 ( $(MAKE) bench-longmemeval-on-2 CONFIG=$(or $(LME_CONFIG),$(CONFIG)) $(if $(MULTI_QUERY),MULTI_QUERY=1,) $(if $(RESUME),RESUME=1,) > $(RESULTS_DIR)/parallel/lme.log 2>&1 ; \
	   echo "  [parallel] lme exited ($$?)" ) & \
	 wait
	@echo "  [parallel] Both done. Results: $(RESULTS_DIR)/latest.json (locomo) + $(RESULTS_DIR)/lme-on-2/latest.json (lme)"

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
		--config $(CONFIG) \
		--benchmarks builtin

# All benchmarks
bench: fetch-datasets bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config $(CONFIG) \
		--benchmarks builtin longmemeval locomo \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--locomo-path $(LOCOMO_DIR)/data

# Full canonical run — LME + LoCoMo in parallel, LLM-judge, competitor-comparable numbers.
# Requires API key (use: doppler run -- make bench-full)
# Pass RESUME=1 to continue an interrupted run: doppler run -- make bench-full RESUME=1
bench-full: fetch-datasets bench-db-start
	DATABASE_URL="$(BENCH_DATABASE_URL)" ASTROCYTE_TASKS_DSN="$(BENCH_DATABASE_URL)" $(BENCH_UV_RUN) python scripts/run_benchmarks.py \
		--config $(CONFIG) \
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

.PHONY: bench-baseline-locomo-fair bench-baseline-longmemeval-quick bench-baseline-gate

bench-baseline-locomo-fair:
	$(MAKE) bench-locomo-fair CONFIG=benchmarks/config-baseline.yaml

bench-baseline-longmemeval-quick:
	$(MAKE) bench-longmemeval-quick CONFIG=benchmarks/config-baseline.yaml

bench-baseline-gate:
	uv run python scripts/check_benchmark_gates.py \
		--gates benchmarks/gates-baseline.json \
		--results $(RESULTS_DIR)/latest.json

# Smoke test — in-memory providers, no API key needed.
# Uses the real LoCoMo dataset with category-stratified sampling so the
# regression gate covers all five categories (multi-hop, temporal,
# open-domain, single-hop, adversarial) without paying for the full
# 1986-question run. With ``--max-questions-per-conversation 5`` we get
# ceil(5/5)=1 question per category per conversation × 10 conversations
# = ~50 questions, every category represented. With the mock provider
# this runs in well under a minute. Bump the per-conv number if a
# regression slips through this sample.
bench-smoke: fetch-locomo
	uv run python scripts/run_benchmarks.py \
		--provider test \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--max-questions-per-conversation 5

# ---------------------------------------------------------------------------
# CodeQL
# ---------------------------------------------------------------------------

CODEQL_DB      := /tmp/astrocyte-codeql-db
CODEQL_OUT     := /tmp/astrocyte-findings.csv
CODEQL_QUERIES := \
	~/.codeql/packages/codeql/python-queries/1.8.0/Statements/StatementNoEffect.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Statements/UnreachableCode.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Statements/AssertOnTuple.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Statements/AssertLiteralConstant.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Statements/SideEffectInAssert.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Statements/RedundantAssignment.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Functions/ConsistentReturns.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Functions/ModificationOfParameterWithDefault.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Imports/ImportandImportFrom.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Imports/UnusedImport.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Exceptions/EmptyExcept.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Exceptions/IncorrectExceptOrder.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Variables/UninitializedLocal.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Variables/UnusedLocalVariable.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Variables/UnusedModuleVariable.ql \
	~/.codeql/packages/codeql/python-queries/1.8.0/Expressions/UnintentionalImplicitStringConcatenation.ql

.PHONY: codeql
codeql:
	codeql database create $(CODEQL_DB) --language=python --source-root . --overwrite
	codeql database analyze $(CODEQL_DB) $(CODEQL_QUERIES) --format=csv --output=$(CODEQL_OUT)
	@echo "--- Findings (excluding datasets/) ---"
	@python3 scripts/filter_codeql.py $(CODEQL_OUT)

# ---------------------------------------------------------------------------
# Bench archive (Cloudflare R2)
# ---------------------------------------------------------------------------
# All targets here require the six R2_* env vars from Doppler config `bench`.
# See docs/_design/bench-archive.md and docs/_plugins/benchmarks-doppler-setup.md.

.PHONY: bench-archive-selftest bench-archive-push bench-archive-fetch \
	bench-archive-trajectory bench-archive-rebuild-trajectory \
	bench-archive-backfill-dry bench-archive-backfill

# STAGE label (defaults to local-ad-hoc) and BENCH selector pass through.
STAGE ?= local-ad-hoc
BENCH ?= locomo

# Round-trip a small object against both buckets to verify creds + CORS reach.
bench-archive-selftest:
	uv run python -m scripts.archive_bench_results --selftest

# Manually archive every results-*.json in benchmark-results/ under STAGE.
# Usually unnecessary: scripts/run_benchmarks.py archives via the post-run
# hook on every successful run. Use this for backfilling a single label.
bench-archive-push:
	uv run python -m scripts.archive_bench_results \
		--stage $(STAGE) \
		--files $(RESULTS_DIR)/results-*.json

# Pull the latest archived run for one bench down to benchmark-results/_r2/.
bench-archive-fetch:
	uv run python -m scripts.fetch_bench_results --bench $(BENCH) --latest

# Pull only the public trajectory artifact (small, useful for CLI trends).
bench-archive-trajectory:
	uv run python -m scripts.fetch_bench_results --bench $(BENCH) --trajectory

# Rebuild trajectory/<bench>.json from manifests (no upload of raw runs).
# Use after `bench-archive-backfill` or if the public bucket is wiped.
bench-archive-rebuild-trajectory:
	uv run python -m scripts.archive_bench_results --rebuild-trajectory

# One-shot historical backfill — preview what would upload (default DRY).
bench-archive-backfill-dry:
	uv run python -m scripts.backfill_archive

# One-shot historical backfill — actually upload everything.
bench-archive-backfill:
	uv run python -m scripts.backfill_archive --execute

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

.PHONY: clean-datasets clean-results

clean-datasets:
	rm -rf $(DATASETS_DIR)

clean-results:
	rm -rf $(RESULTS_DIR)
