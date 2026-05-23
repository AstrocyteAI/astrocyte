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
BENCH_PG_USER      := astrocyte
BENCH_PG_PASSWORD  := astrocyte
BENCH_PG_DB        := astrocyte_bench
BENCH_EMBEDDING_DIMENSIONS := 1536
BENCH_DATABASE_URL := postgresql://$(BENCH_PG_USER):$(BENCH_PG_PASSWORD)@127.0.0.1:$(BENCH_PG_PORT)/$(BENCH_PG_DB)
# Bench runs need the full adapter stack (Postgres, ingestion connectors,
# OpenAI, aiobotocore) plus the cross-encoder reranker. Pre-0.14 these were
# all in ``dev``; after the split ``dev`` is contributor-slim and ``bench``
# is the full bench stack. See pyproject.toml ``[project.optional-dependencies]``.
BENCH_UV_RUN := uv run --extra bench --extra rerank
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
# DB contention or bank-id collisions. PageIndex bank rows persist across
# runs (resume semantics), so two containers on different ports let each
# leg keep its own warm cache.
#
# Run pattern:
#   Single command:  make bench-parallel
#   Or two terminals:
#     Terminal A: make bench-locomo            (DB on :5433)
#     Terminal B: make bench-longmemeval-on-2  (DB on :5434)
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
#
# DB lifecycle knob (BENCH_RESET, 2026-05-14):
#
#   BENCH_RESET=1   (default)  reset the bench DBs before every run
#   BENCH_RESET=0              start the bench DBs but keep existing rows
#                              (resume PageIndex bank-id cache, fast iter)
#
# Rationale for reset-by-default: without a reset, rows from prior bench
# runs accumulate across the Postgres schema even though each run uses a
# fresh ``bank_id``. After ~10 runs the query planner degraded and
# per-question latency exploded from ~10s to ~315s in practice.
#
# When to opt into BENCH_RESET=0:
#   - Iterating on code where you want the PageIndex bench's resume
#     semantics (same --bank-id → skip re-extraction)
#   - Debugging a specific question without re-paying the extraction cost
#   - You've recently reset and just want a quick second run
#
# Examples:
#   make bench-mem0-harness-parallel MEM0_HARNESS_PROJECT=foo            # resets
#   make bench-mem0-harness-parallel MEM0_HARNESS_PROJECT=foo BENCH_RESET=0  # resumes
#   make bench-locomo BENCH_RESET=0 LOCOMO_BANK_ID=my-cache              # resume bank
BENCH_RESET ?= 1

ifeq ($(BENCH_RESET),1)
_BENCH_DB   := bench-db-reset
_BENCH_DB_2 := bench-db-reset-2
else
_BENCH_DB   := bench-db-start
_BENCH_DB_2 := bench-db-start-2
endif

.PHONY: bench-db-start bench-db-stop bench-db-reset bench-db-start-2 bench-db-stop-2 bench-db-reset-2 bench-locomo bench-longmemeval bench-longmemeval-on-2 bench-parallel

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
			docker build -t $(BENCH_PG_IMAGE) -f ../docker/astrocyte-postgres/Dockerfile ..; \
			BENCH_IMG=$(BENCH_PG_IMAGE); \
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
			docker build -t $(BENCH_PG_IMAGE) -f ../docker/astrocyte-postgres/Dockerfile ..; \
			BENCH_IMG=$(BENCH_PG_IMAGE); \
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

# ---------------------------------------------------------------------------
# Benchmarks — PageIndex canonical harness (v1.x)
# ---------------------------------------------------------------------------
#
# The v0.x harness (scripts/run_benchmarks.py + benchmarks/config-*.yaml) was
# deprecated and removed in May 2026 in favour of PageIndex section-grain +
# fact-grain retrieval (M9–M12). `bench-locomo` / `bench-longmemeval` /
# `bench-parallel` now run PageIndex. `bench-archive` ships the canonical
# pair of LME+LoCoMo result JSONs to R2 under a single STAGE label.
#
# Targets:
#   bench-locomo              LoCoMo, primary DB :5433
#   bench-longmemeval         LongMemEval-S, primary DB :5433
#   bench-longmemeval-on-2    LongMemEval-S, secondary DB :5434 (for parallel)
#   bench-parallel            LoCoMo on :5433 + LME on :5434, true parallel
#   bench-archive STAGE=foo   Push latest LME + LoCoMo JSON to R2
#
# Knobs:
#   LOCOMO_MAX_Q=N            questions per LoCoMo conversation (default 20 → 200 total)
#   LME_MAX_SAMPLES=N         LME samples (default 200, full set = 500)
#
# All targets self-bootstrap Doppler (`doppler run -- env DATABASE_URL=... uv ...`).
# DBs are auto-started via bench-db-start / bench-db-start-2 deps.
# Resume semantics: PageIndex bank persistence IS resume — re-running with
# the same --bank-id skips re-extraction of any docs already in the bank.
# No RESUME flag needed.

LOCOMO_MAX_Q     ?= 20
LME_MAX_SAMPLES  ?= 200
LOCOMO_BANK_ID   ?= bench-pageindex-locomo
LME_BANK_ID      ?= bench-pageindex-lme

# Bench-runner deps. These are NOT in the [bench] extra in pyproject.toml because:
#   - PageIndex (vendored at /Users/calvin/AstrocyteAI/PageIndex) — needs litellm,
#     PyPDF2, pymupdf. Sys.path-imported, not a pyproject dep.
#   - memory-benchmarks (vendored at /Users/calvin/AstrocyteAI/memory-benchmarks) —
#     needs aiolimiter, anthropic. Also sys.path-imported.
#   - astrocyte-llm-litellm — incompatible with the ``mcp`` extra via
#     litellm's dotenv pin (see comment in pyproject.toml ``[project.optional-dependencies]``).
#
# Bench targets below depend on ``bench-runner-deps``, which installs all of these
# imperatively via ``uv pip install``. Idempotent — re-running is cheap.
# Triggered automatically by every bench-* target; runs a smoke import that fails
# fast if anything's missing.
.PHONY: bench-runner-deps
bench-runner-deps:
	@uv run python -c "import litellm, PyPDF2, pymupdf, aiolimiter, anthropic" 2>/dev/null || ( \
		echo "  [bench-runner-deps] installing missing runner deps..."; \
		uv pip install -q --editable ../adapters-llm-py/astrocyte-llm-litellm; \
		uv pip install -q PyPDF2 pymupdf aiolimiter; \
		uv pip install -q -r /Users/calvin/AstrocyteAI/memory-benchmarks/requirements.txt; \
		echo "  [bench-runner-deps] done"; \
	)

.PHONY: bench-locomo bench-longmemeval bench-longmemeval-on-2 bench-parallel

bench-locomo: fetch-locomo $(_BENCH_DB) bench-runner-deps
	@mkdir -p $(RESULTS_DIR)/pageindex/locomo
	doppler run -- env DATABASE_URL='$(BENCH_DATABASE_URL)' ASTROCYTE_PG_DSN='$(BENCH_DATABASE_URL)' ASTROCYTE_MAX_TOKENS_CUTOFFS='$(MEM0_HARNESS_TOKEN_CUTOFFS)' \
	  uv run python scripts/bench_pageindex_locomo.py \
	    --backend postgres \
	    --max-questions-per-conversation $(LOCOMO_MAX_Q) \
	    --bank-id $(LOCOMO_BANK_ID)

bench-longmemeval: fetch-longmemeval $(_BENCH_DB) bench-runner-deps
	@mkdir -p $(RESULTS_DIR)/pageindex/lme
	doppler run -- env DATABASE_URL='$(BENCH_DATABASE_URL)' ASTROCYTE_PG_DSN='$(BENCH_DATABASE_URL)' ASTROCYTE_MAX_TOKENS_CUTOFFS='$(MEM0_HARNESS_TOKEN_CUTOFFS)' \
	  uv run python scripts/bench_pageindex_lme.py \
	    --backend postgres \
	    --max-samples $(LME_MAX_SAMPLES) \
	    --bank-id $(LME_BANK_ID)

bench-longmemeval-on-2: fetch-longmemeval $(_BENCH_DB_2) bench-runner-deps
	@mkdir -p $(RESULTS_DIR)/pageindex/lme
	doppler run -- env DATABASE_URL='$(BENCH_DATABASE_URL_2)' ASTROCYTE_PG_DSN='$(BENCH_DATABASE_URL_2)' ASTROCYTE_MAX_TOKENS_CUTOFFS='$(MEM0_HARNESS_TOKEN_CUTOFFS)' \
	  uv run python scripts/bench_pageindex_lme.py \
	    --backend postgres \
	    --max-samples $(LME_MAX_SAMPLES) \
	    --bank-id $(LME_BANK_ID)

# True parallel: LoCoMo on :5433 + LME on :5434. Each leg gets its own
# Postgres container, Python process, and log file — one crash does not
# affect the other. `wait` returns only when both children finish.
#
# Live progress: tail -f $(RESULTS_DIR)/parallel/{locomo,lme}.log
#
# Cache state: bank rows persist across runs. First run on :5434 for
# LME will be a cold start (~2-3hr full extraction). Subsequent runs
# hit cache and only re-score (~30-60min).
bench-parallel: fetch-datasets $(_BENCH_DB) $(_BENCH_DB_2) bench-runner-deps
	@mkdir -p $(RESULTS_DIR)/pageindex/locomo $(RESULTS_DIR)/pageindex/lme $(RESULTS_DIR)/parallel
	@echo "  [parallel] LoCoMo on :5433 + LME on :5434"
	@echo "  [parallel] Logs: $(RESULTS_DIR)/parallel/{locomo,lme}.log"
	@echo "  [parallel] Watch live: tail -f $(RESULTS_DIR)/parallel/locomo.log"
	@( doppler run -- env DATABASE_URL='$(BENCH_DATABASE_URL)' ASTROCYTE_PG_DSN='$(BENCH_DATABASE_URL)' ASTROCYTE_MAX_TOKENS_CUTOFFS='$(MEM0_HARNESS_TOKEN_CUTOFFS)' \
	     uv run python scripts/bench_pageindex_locomo.py --backend postgres \
	       --max-questions-per-conversation $(LOCOMO_MAX_Q) --bank-id $(LOCOMO_BANK_ID) \
	     > $(RESULTS_DIR)/parallel/locomo.log 2>&1 ; \
	     echo "  [parallel] locomo exited ($$?)" ) & \
	 ( doppler run -- env DATABASE_URL='$(BENCH_DATABASE_URL_2)' ASTROCYTE_PG_DSN='$(BENCH_DATABASE_URL_2)' ASTROCYTE_MAX_TOKENS_CUTOFFS='$(MEM0_HARNESS_TOKEN_CUTOFFS)' \
	     uv run python scripts/bench_pageindex_lme.py --backend postgres \
	       --max-samples $(LME_MAX_SAMPLES) --bank-id $(LME_BANK_ID) \
	     > $(RESULTS_DIR)/parallel/lme.log 2>&1 ; \
	     echo "  [parallel] lme exited ($$?)" ) & \
	 wait
	@echo "  [parallel] Both done. Results: $(RESULTS_DIR)/pageindex/{locomo,lme}/"

# ---------------------------------------------------------------------------
# Mem0-harness benchmarks (M13.1)
# ---------------------------------------------------------------------------
#
# Runs Astrocyte through the upstream Mem0 memory-benchmarks harness for
# apples-to-apples comparison against Mem0's own published numbers.
# Uses the same dataset loader, judge prompt, and metric aggregation as
# Mem0's runners; only swaps Mem0Client → AstrocyteClient.
#
# Knobs:
#   MEM0_HARNESS_PROJECT  project label written into results JSON
#   MEM0_HARNESS_MAX_WORKERS  parallel conversations / questions (default 4)
#   MEM0_HARNESS_TOP_K        candidate list depth (default 200)
#   MEM0_HARNESS_USER_PROFILE pass --user-profile when set (default on)
#
# Both legs require Postgres + Doppler. LoCoMo lands on :5433, LME on
# :5434 — distinct DBs so they don't contend on a shared bank-id
# namespace. The AstrocyteClient subclasses already prefix banks with
# m13.1.locomo / m13.1.lme so even on a single DB they wouldn't collide,
# but the two-DB pattern matches our native bench-parallel.

MEM0_HARNESS_PROJECT          ?= astrocyte-m13.2
MEM0_HARNESS_MAX_WORKERS      ?= 4
MEM0_HARNESS_TOP_K            ?= 200
# M35-4 — cutoff values passed as --top-k-cutoffs. When
# MEM0_HARNESS_TOKEN_CUTOFFS is set, those values are token budgets
# (and the run_*.py wrappers install a patch that interprets the
# framework's "top-k" loop as token-budget slicing). Default values
# (10,20,50,200) preserve pre-M35 item-count cutoff reporting.
MEM0_HARNESS_TOKEN_CUTOFFS    ?= 1024,2048,4096,8192
MEM0_HARNESS_CUTOFFS          ?= $(MEM0_HARNESS_TOKEN_CUTOFFS)
MEM0_HARNESS_USER_PROFILE     ?= 1
# Default to gpt-4o-mini — matches Hindsight's AMB defaults
# (hindsight-dev/benchmarks/common/benchmark_runner.py:228) so our numbers
# are directly comparable to Hindsight's published 92-94%. ~16× cheaper
# than gpt-4o per question. Override to gpt-4o for apples-to-apples with
# Mem0's paper Table 1 (which uses gpt-4o).
MEM0_HARNESS_ANSWERER_MODEL   ?= gpt-4o-mini
MEM0_HARNESS_JUDGE_MODEL      ?= gpt-4o-mini

# Question-cap defaults — standing methodology going forward:
#   LOCOMO_MAX_Q  total-question cap for LoCoMo (run_locomo.py passes this
#                 as ``--max-questions N`` — TOTAL across all 10 convs,
#                 NOT per-conv). 200 is the published Mem0/LoCoMo subset
#                 convention and the anchor we use in BENCH_PARITY.yaml
#                 for cross-release comparability with v0.14.0. Override
#                 to empty to run the full 1540-q dataset (expensive —
#                 ~2-3 hrs; only for final-cycle statistical-confidence
#                 runs, not iteration). v015w accidentally ran at 1540
#                 because this var was empty — see the v0.15.0 ship-
#                 decision memo §"LoCoMo n=1540 footnote".
#   LME_PER_TYPE  per-question-type cap (15 × 6 types = 90 total).
#                 Standing default per the ship-decision memo §"LME
#                 measurement cadence". Reserved escalation: 30 (n=180)
#                 for high-confidence cycle-close benches; leave empty
#                 for harness default (--per-type 5, n=30).
MEM0_HARNESS_LOCOMO_MAX_Q     ?= 200
MEM0_HARNESS_LME_PER_TYPE     ?= 15

.PHONY: bench-mem0-harness-locomo bench-mem0-harness-lme bench-mem0-harness-parallel

_MEM0_HARNESS_USER_PROFILE_FLAG := $(if $(MEM0_HARNESS_USER_PROFILE),--user-profile,)
_MEM0_HARNESS_LOCOMO_MAX_Q_FLAG := $(if $(MEM0_HARNESS_LOCOMO_MAX_Q),--max-questions $(MEM0_HARNESS_LOCOMO_MAX_Q),)
_MEM0_HARNESS_LME_PER_TYPE_FLAG := $(if $(MEM0_HARNESS_LME_PER_TYPE),--per-type $(MEM0_HARNESS_LME_PER_TYPE),)

bench-mem0-harness-locomo: $(_BENCH_DB) bench-runner-deps
	@mkdir -p $(RESULTS_DIR)/mem0_harness/locomo
	doppler run -- env DATABASE_URL='$(BENCH_DATABASE_URL)' ASTROCYTE_PG_DSN='$(BENCH_DATABASE_URL)' ASTROCYTE_MAX_TOKENS_CUTOFFS='$(MEM0_HARNESS_TOKEN_CUTOFFS)' \
	  uv run python scripts/mem0_harness/run_locomo.py \
	    --project-name $(MEM0_HARNESS_PROJECT) --backend oss \
	    --judge-model $(MEM0_HARNESS_JUDGE_MODEL) --judge-provider openai \
	    --answerer-model $(MEM0_HARNESS_ANSWERER_MODEL) --provider openai \
	    --max-workers $(MEM0_HARNESS_MAX_WORKERS) --rpm 60 \
	    --top-k $(MEM0_HARNESS_TOP_K) --top-k-cutoffs $(MEM0_HARNESS_CUTOFFS) \
	    $(_MEM0_HARNESS_USER_PROFILE_FLAG) \
	    $(_MEM0_HARNESS_LOCOMO_MAX_Q_FLAG) \
	    --output-dir $(RESULTS_DIR)/mem0_harness/locomo/$(MEM0_HARNESS_PROJECT)

bench-mem0-harness-lme: $(_BENCH_DB_2) bench-runner-deps
	@mkdir -p $(RESULTS_DIR)/mem0_harness/lme
	doppler run -- env DATABASE_URL='$(BENCH_DATABASE_URL_2)' ASTROCYTE_PG_DSN='$(BENCH_DATABASE_URL_2)' ASTROCYTE_MAX_TOKENS_CUTOFFS='$(MEM0_HARNESS_TOKEN_CUTOFFS)' \
	  uv run python scripts/mem0_harness/run_lme.py \
	    --project-name $(MEM0_HARNESS_PROJECT) --backend oss \
	    --judge-model $(MEM0_HARNESS_JUDGE_MODEL) --judge-provider openai \
	    --answerer-model $(MEM0_HARNESS_ANSWERER_MODEL) --provider openai \
	    --max-workers $(MEM0_HARNESS_MAX_WORKERS) --rpm 60 \
	    --top-k $(MEM0_HARNESS_TOP_K) --top-k-cutoffs $(MEM0_HARNESS_CUTOFFS) \
	    $(_MEM0_HARNESS_USER_PROFILE_FLAG) \
	    $(_MEM0_HARNESS_LME_PER_TYPE_FLAG) \
	    --output-dir $(RESULTS_DIR)/mem0_harness/lme/$(MEM0_HARNESS_PROJECT)

# True parallel: LoCoMo on :5433 + LME on :5434, distinct logs, single
# command. Same shape as bench-parallel for the native PageIndex bench.
bench-mem0-harness-parallel: $(_BENCH_DB) $(_BENCH_DB_2) bench-runner-deps
	@mkdir -p $(RESULTS_DIR)/mem0_harness/locomo $(RESULTS_DIR)/mem0_harness/lme $(RESULTS_DIR)/parallel
	@echo "  [mem0-parallel] LoCoMo on :5433 + LME on :5434 (project=$(MEM0_HARNESS_PROJECT))"
	@echo "  [mem0-parallel] judge=$(MEM0_HARNESS_JUDGE_MODEL)  answerer=$(MEM0_HARNESS_ANSWERER_MODEL)"
	@echo "  [mem0-parallel] Logs: $(RESULTS_DIR)/parallel/mem0_{locomo,lme}.log"
	@( doppler run -- env DATABASE_URL='$(BENCH_DATABASE_URL)' ASTROCYTE_PG_DSN='$(BENCH_DATABASE_URL)' ASTROCYTE_MAX_TOKENS_CUTOFFS='$(MEM0_HARNESS_TOKEN_CUTOFFS)' \
	     uv run python scripts/mem0_harness/run_locomo.py \
	       --project-name $(MEM0_HARNESS_PROJECT) --backend oss \
	       --judge-model $(MEM0_HARNESS_JUDGE_MODEL) --judge-provider openai \
	       --answerer-model $(MEM0_HARNESS_ANSWERER_MODEL) --provider openai \
	       --max-workers $(MEM0_HARNESS_MAX_WORKERS) --rpm 60 \
	       --top-k $(MEM0_HARNESS_TOP_K) --top-k-cutoffs $(MEM0_HARNESS_CUTOFFS) \
	       $(_MEM0_HARNESS_USER_PROFILE_FLAG) \
	       $(_MEM0_HARNESS_LOCOMO_MAX_Q_FLAG) \
	       --output-dir $(RESULTS_DIR)/mem0_harness/locomo/$(MEM0_HARNESS_PROJECT) \
	     > $(RESULTS_DIR)/parallel/mem0_locomo.log 2>&1 ; \
	     echo "  [mem0-parallel] locomo exited ($$?)" ) & \
	 ( doppler run -- env DATABASE_URL='$(BENCH_DATABASE_URL_2)' ASTROCYTE_PG_DSN='$(BENCH_DATABASE_URL_2)' ASTROCYTE_MAX_TOKENS_CUTOFFS='$(MEM0_HARNESS_TOKEN_CUTOFFS)' \
	     uv run python scripts/mem0_harness/run_lme.py \
	       --project-name $(MEM0_HARNESS_PROJECT) --backend oss \
	       --judge-model $(MEM0_HARNESS_JUDGE_MODEL) --judge-provider openai \
	       --answerer-model $(MEM0_HARNESS_ANSWERER_MODEL) --provider openai \
	       --max-workers $(MEM0_HARNESS_MAX_WORKERS) --rpm 60 \
	       --top-k $(MEM0_HARNESS_TOP_K) --top-k-cutoffs $(MEM0_HARNESS_CUTOFFS) \
	       $(_MEM0_HARNESS_USER_PROFILE_FLAG) \
	       $(_MEM0_HARNESS_LME_PER_TYPE_FLAG) \
	       --output-dir $(RESULTS_DIR)/mem0_harness/lme/$(MEM0_HARNESS_PROJECT) \
	     > $(RESULTS_DIR)/parallel/mem0_lme.log 2>&1 ; \
	     echo "  [mem0-parallel] lme exited ($$?)" ) & \
	 wait
	@echo "  [mem0-parallel] Both done. Results: $(RESULTS_DIR)/mem0_harness/{locomo,lme}/$(MEM0_HARNESS_PROJECT)/"

# Archive the LATEST Mem0-harness result JSON for BOTH benches under
# a single STAGE label. Picks the most recent file from each
# benchmark-results/mem0_harness/<bench>/$(MEM0_HARNESS_PROJECT)/
# directory and uploads with the matching --bench override.
#
# Usage: make bench-archive-mem0 STAGE=m13-close
#        make bench-archive-mem0 STAGE=m14-1-close MEM0_HARNESS_PROJECT=astrocyte-m14.1
.PHONY: bench-archive-mem0
bench-archive-mem0:
	@latest_lme="$$(ls -t $(RESULTS_DIR)/mem0_harness/lme/$(MEM0_HARNESS_PROJECT)/longmemeval_results_*.json 2>/dev/null | head -1)"; \
	latest_locomo="$$(ls -t $(RESULTS_DIR)/mem0_harness/locomo/$(MEM0_HARNESS_PROJECT)/locomo_results_*.json 2>/dev/null | head -1)"; \
	if [ -z "$$latest_lme" ]; then echo "no LME results in $(RESULTS_DIR)/mem0_harness/lme/$(MEM0_HARNESS_PROJECT)/" >&2; exit 1; fi; \
	if [ -z "$$latest_locomo" ]; then echo "no LoCoMo results in $(RESULTS_DIR)/mem0_harness/locomo/$(MEM0_HARNESS_PROJECT)/" >&2; exit 1; fi; \
	echo "archiving stage=$(STAGE) project=$(MEM0_HARNESS_PROJECT)"; \
	echo "  LME    -> $$latest_lme"; \
	echo "  LoCoMo -> $$latest_locomo"; \
	doppler run --config bench -- $(BENCH_UV_RUN) python -m scripts.archive_bench_results --stage $(STAGE) --bench longmemeval --files "$$latest_lme" && \
	doppler run --config bench -- $(BENCH_UV_RUN) python -m scripts.archive_bench_results --stage $(STAGE) --bench locomo --files "$$latest_locomo"

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

.PHONY: bench-archive bench-archive-selftest bench-archive-fetch \
	bench-archive-trajectory bench-archive-rebuild-trajectory \
	bench-archive-backfill-dry bench-archive-backfill

# STAGE label (defaults to local-ad-hoc) and BENCH selector pass through.
STAGE ?= local-ad-hoc
BENCH ?= locomo

# Round-trip a small object against both buckets to verify creds + CORS reach.
bench-archive-selftest:
	uv run python -m scripts.archive_bench_results --selftest

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

# Walk benchmark-results/ and archive every project lacking an _ARCHIVED
# marker. Stage name is derived from the project directory; bench from
# the file's metadata or path. Idempotent — already-archived projects are
# skipped. The canonical sweep target — prefer this over the legacy
# `bench-archive STAGE=foo --files ...` flow for cycle-level archiving.
#
# Common variants:
#   make bench-archive-rescan                  # default cutoff = script default
#                                              #   (currently max_tokens_8192
#                                              #   with fallback to top_20)
#   make bench-archive-rescan DRY=1            # preview without uploading
#   make bench-archive-rescan MEM0_CUTOFF=top_20  # pin a specific cutoff
#   make bench-archive-rescan FORCE=1          # re-archive even with marker
#   make bench-archive-rescan INCLUDE_SMOKE=1  # don't skip smoke / n<30 runs

# Curation — mark a benchmark project as part of a SHIPPED cycle.
# The trajectory regenerator picks the most-recent ship_label group and
# means its overall scores for the README badge. Usage::
#
#   make bench-mark-shipped PROJECT=m18b-b1-dp-rrf-run-1 LABEL=m18b \
#       RATIONALE="B1-dp+RRF: dateparser Pass B + RRF fact fusion"
#
# Typical cycle close marks 2 replicate projects with the same label.
# After marking, re-run `make bench-archive-rescan` to propagate the
# label into the manifest (and from there into the public badge).
bench-mark-shipped:
	@[ -n "$(PROJECT)" ] || { echo "ERROR: PROJECT=<project-dir-name> required" >&2; exit 2; }
	@[ -n "$(LABEL)" ] || { echo "ERROR: LABEL=<cycle-label> required (e.g. m18b)" >&2; exit 2; }
	uv run python -m scripts.mark_shipped \
		--project $(PROJECT) \
		--label $(LABEL) \
		$(if $(RATIONALE),--rationale "$(RATIONALE)",) \
		$(if $(FORCE),--force,) \
		$(if $(ALLOW_UNARCHIVED),--allow-unarchived,)

bench-mark-unshipped:
	@[ -n "$(PROJECT)" ] || { echo "ERROR: PROJECT=<project-dir-name> required" >&2; exit 2; }
	uv run python -m scripts.mark_shipped --project $(PROJECT) --unmark

# After `bench-mark-shipped` (and again after `bench-mark-unshipped`),
# patch ship_label fields into already-archived manifest entries and
# regenerate the public badges. Doesn't re-upload result content.
bench-refresh-labels:
	doppler run --config bench -- $(BENCH_UV_RUN) python -m scripts.archive_bench_results \
		--refresh-labels \
		$(if $(DRY),--dry-run,)

# Record a released package version's bench-cycle parity. Appends a row
# to BENCH_PARITY.yaml at the repo root (read by the README badge writer
# so the badge displays the released package's score, not whatever cycle
# ran last). Optionally creates the corresponding annotated release tag.
#
# Required: PACKAGE=<name> VERSION=X.Y.Z CYCLE=<bench-label>
# Optional: TAG=1 to also create <package>-v<VERSION> at HEAD
#           DRY=1 to preview, FORCE=1 to overwrite existing entry, ALLOW_DIRTY=1
#
#   make release-mark PACKAGE=astrocyte VERSION=0.13.0 CYCLE=m18b TAG=1
release-mark:
	@[ -n "$(PACKAGE)" ] || { echo "ERROR: PACKAGE=<name> required" >&2; exit 2; }
	@[ -n "$(VERSION)" ] || { echo "ERROR: VERSION=X.Y.Z required" >&2; exit 2; }
	@[ -n "$(CYCLE)" ] || { echo "ERROR: CYCLE=<bench-label> required" >&2; exit 2; }
	uv run python -m scripts.release_mark \
		--package $(PACKAGE) \
		--version $(VERSION) \
		--cycle $(CYCLE) \
		$(if $(TAG),--tag,) \
		$(if $(COMMIT),--commit $(COMMIT),) \
		$(if $(DRY),--dry-run,) \
		$(if $(FORCE),--force,) \
		$(if $(ALLOW_DIRTY),--allow-dirty,)

# Lockstep release-mark — same VERSION + CYCLE applied to all three
# user-facing ship surfaces (astrocyte / astrocyte-postgres /
# astrocyte-gateway-py). Override by setting RELEASE_PACKAGES if you
# want a different list this cycle (e.g. RELEASE_PACKAGES="astrocyte
# astrocyte-postgres astrocyte-gateway-py astrocyte-qdrant").
#
#   make release-mark-all VERSION=0.13.0 CYCLE=m18b TAG=1
RELEASE_PACKAGES ?= astrocyte astrocyte-postgres astrocyte-gateway-py
release-mark-all:
	@[ -n "$(VERSION)" ] || { echo "ERROR: VERSION=X.Y.Z required" >&2; exit 2; }
	@[ -n "$(CYCLE)" ] || { echo "ERROR: CYCLE=<bench-label> required" >&2; exit 2; }
	@for pkg in $(RELEASE_PACKAGES); do \
		echo "===  $$pkg v$(VERSION) <- $(CYCLE)  ==="; \
		$(MAKE) --no-print-directory release-mark \
			PACKAGE=$$pkg VERSION=$(VERSION) CYCLE=$(CYCLE) \
			TAG=$(TAG) COMMIT=$(COMMIT) DRY=$(DRY) FORCE=$(FORCE) ALLOW_DIRTY=$(ALLOW_DIRTY) \
			|| exit $$?; \
	done

# Create an annotated git tag bench/<LABEL> anchoring a shipped cycle.
# Reads the labelled project dirs locally to compose the tag message
# (scores + rationale + run names). Refuses if working tree is dirty
# unless ALLOW_DIRTY=1.
#
#   make bench-tag-shipped LABEL=m18b                  # tag HEAD
#   make bench-tag-shipped LABEL=m18b COMMIT=a1b2c3d   # retroactive
#   make bench-tag-shipped LABEL=m18b DRY=1            # preview message
#   make bench-tag-shipped LABEL=m18b FORCE=1          # overwrite
#
# Tag is NOT pushed automatically. Push when ready:
#   git push origin bench/<LABEL>
bench-tag-shipped:
	@[ -n "$(LABEL)" ] || { echo "ERROR: LABEL=<cycle-label> required" >&2; exit 2; }
	uv run python -m scripts.tag_shipped \
		--label $(LABEL) \
		$(if $(COMMIT),--commit $(COMMIT),) \
		$(if $(DRY),--dry-run,) \
		$(if $(FORCE),--force,) \
		$(if $(ALLOW_DIRTY),--allow-dirty,)

bench-archive-rescan:
	doppler run --config bench -- $(BENCH_UV_RUN) python -m scripts.archive_bench_results \
		--rescan \
		$(if $(MEM0_CUTOFF),--mem0-cutoff $(MEM0_CUTOFF),) \
		$(if $(DRY),--dry-run,) \
		$(if $(FORCE),--force,) \
		$(if $(INCLUDE_SMOKE),--include-smoke,)

# One-shot historical backfill — preview what would upload (default DRY).
bench-archive-backfill-dry:
	uv run python -m scripts.backfill_archive

# One-shot historical backfill — actually upload everything.
bench-archive-backfill:
	uv run python -m scripts.backfill_archive --execute

# Archive the LATEST result JSON for BOTH PageIndex benches (LME + LoCoMo)
# under a single STAGE label. Picks the most recent file from each
# benchmark-results/pageindex/<bench>/ subdirectory and uploads with the
# matching --bench override (results are "unwrapped" — bench name isn't
# inside the JSON, so the archive script needs it on the CLI).
#
#   make bench-archive STAGE=m12-close
#
# Fails the whole target if either upload fails; partial archives are
# undesirable for trajectory continuity.
bench-archive:
	@latest_lme="$$(ls -t $(RESULTS_DIR)/pageindex/lme/results-*.json 2>/dev/null | head -1)"; \
	latest_locomo="$$(ls -t $(RESULTS_DIR)/pageindex/locomo/results-*.json 2>/dev/null | head -1)"; \
	if [ -z "$$latest_lme" ]; then echo "no LME results in $(RESULTS_DIR)/pageindex/lme/" >&2; exit 1; fi; \
	if [ -z "$$latest_locomo" ]; then echo "no LoCoMo results in $(RESULTS_DIR)/pageindex/locomo/" >&2; exit 1; fi; \
	echo "archiving stage=$(STAGE)"; \
	echo "  LME    -> $$latest_lme"; \
	echo "  LoCoMo -> $$latest_locomo"; \
	doppler run --config bench -- $(BENCH_UV_RUN) python -m scripts.archive_bench_results --stage $(STAGE) --bench longmemeval --files "$$latest_lme" && \
	doppler run --config bench -- $(BENCH_UV_RUN) python -m scripts.archive_bench_results --stage $(STAGE) --bench locomo --files "$$latest_locomo"

# ---------------------------------------------------------------------------
# FinanceBench — Document Engine bench (port 5435, no Doppler)
# ---------------------------------------------------------------------------
#
# Tests Astrocyte's Document Engine against FinanceBench (patronus-ai/
# financebench, ~150 Q&A pairs from 10-K SEC filings).
# Reference: Mafin2.5 (VectifyAI) achieved 98.7% via PageIndex tree-search.
#
# Port 5435 — isolated from LME (:5433) and LoCoMo (:5434).
# No Doppler required: credentials passed directly via env vars.
#
# Knobs:
#   FINANCE_STRATEGY      vector (baseline) | tree_search (Phase C/D)
#   FINANCE_MAX_Q         cap on questions (0 = all ~150)
#   FINANCE_PROJECT       label written into results JSON
#   FINANCE_ANSWERER_MODEL  model for answer generation (default gpt-4o-mini)
#   FINANCE_JUDGE_MODEL     model for LLM-as-judge scoring (default gpt-4o-mini)
#
# Usage:
#   make fetch-financebench                              # one-time dataset clone
#   make bench-financebench FINANCE_STRATEGY=vector      # vector baseline
#   make bench-financebench FINANCE_STRATEGY=tree_search # tree-search (Phase C/D)
#   make bench-financebench FINANCE_MAX_Q=10             # smoke (10 questions)

BENCH_FINANCE_CONTAINER    := astrocyte-bench-finance
BENCH_FINANCE_PORT         := 5435
BENCH_FINANCE_DATABASE_URL := postgresql://$(BENCH_PG_USER):$(BENCH_PG_PASSWORD)@127.0.0.1:$(BENCH_FINANCE_PORT)/$(BENCH_PG_DB)
FINANCE_DIR                := $(DATASETS_DIR)/financebench

FINANCE_STRATEGY           ?= vector
FINANCE_MAX_Q              ?= 0
FINANCE_PROJECT            ?= financebench
FINANCE_ANSWERER_MODEL     ?= gpt-4o-mini
FINANCE_JUDGE_MODEL        ?= gpt-4o-mini

.PHONY: fetch-financebench bench-finance-db-start bench-finance-db-stop \
        bench-finance-db-reset bench-financebench bench-archive-financebench

fetch-financebench: $(FINANCE_DIR)/data/financebench_open_source.jsonl
$(FINANCE_DIR)/data/financebench_open_source.jsonl:
	@echo "  [finance] Cloning patronus-ai/financebench..."
	@mkdir -p $(DATASETS_DIR)
	git clone --depth 1 https://github.com/patronus-ai/financebench.git $(FINANCE_DIR)
	@echo "  [finance] Dataset ready at $(FINANCE_DIR)/"

# Start the FinanceBench Postgres container on port 5435.
# Body mirrors bench-db-start / bench-db-start-2 exactly — intentionally
# duplicated for grep-ability (ops file discipline, see comment above).
bench-finance-db-start:
	@set -e; \
	if docker ps --format '{{.Names}}' | grep -q '^$(BENCH_FINANCE_CONTAINER)$$'; then \
		echo "  [bench-finance-db] $(BENCH_FINANCE_CONTAINER) already running"; \
	elif docker ps -a --format '{{.Names}}' | grep -q '^$(BENCH_FINANCE_CONTAINER)$$'; then \
		echo "  [bench-finance-db] Restarting stopped container $(BENCH_FINANCE_CONTAINER)"; \
		docker start $(BENCH_FINANCE_CONTAINER); \
		until docker exec $(BENCH_FINANCE_CONTAINER) pg_isready -U $(BENCH_PG_USER) -d $(BENCH_PG_DB) -q 2>/dev/null; do sleep 1; done; \
		until PGPASSWORD=$(BENCH_PG_PASSWORD) psql "$(BENCH_FINANCE_DATABASE_URL)" -Atc "SELECT 1" >/dev/null 2>&1; do sleep 1; done; \
		echo "  [bench-finance-db] Postgres ready"; \
	else \
		echo "  [bench-finance-db] Pulling $(BENCH_PG_IMAGE)..."; \
		if docker pull $(BENCH_PG_IMAGE); then \
			BENCH_IMG=$(BENCH_PG_IMAGE); \
		else \
			echo "  [bench-finance-db] Pull failed — building from ../docker/astrocyte-postgres/Dockerfile"; \
			docker build -t $(BENCH_PG_IMAGE) -f ../docker/astrocyte-postgres/Dockerfile ..; \
			BENCH_IMG=$(BENCH_PG_IMAGE); \
		fi; \
		docker run -d --name $(BENCH_FINANCE_CONTAINER) \
			-e POSTGRES_USER=$(BENCH_PG_USER) \
			-e POSTGRES_PASSWORD=$(BENCH_PG_PASSWORD) \
			-e POSTGRES_DB=$(BENCH_PG_DB) \
			-p $(BENCH_FINANCE_PORT):5432 \
			$$BENCH_IMG; \
		echo "  [bench-finance-db] Waiting for Postgres to be ready..."; \
		until docker exec $(BENCH_FINANCE_CONTAINER) pg_isready -U $(BENCH_PG_USER) -d $(BENCH_PG_DB) -q 2>/dev/null; do sleep 1; done; \
		until PGPASSWORD=$(BENCH_PG_PASSWORD) psql "$(BENCH_FINANCE_DATABASE_URL)" -Atc "SELECT 1" >/dev/null 2>&1; do sleep 1; done; \
		echo "  [bench-finance-db] Postgres ready at localhost:$(BENCH_FINANCE_PORT)"; \
	fi; \
	echo "  [bench-finance-db] Applying $(BENCH_VECTOR_EXTENSION) migrations (vector($(BENCH_EMBEDDING_DIMENSIONS)))"; \
	ASTROCYTE_EMBEDDING_DIMENSIONS=$(BENCH_EMBEDDING_DIMENSIONS) \
		VECTOR_EXTENSION=$(BENCH_VECTOR_EXTENSION) \
		DATABASE_URL="$(BENCH_FINANCE_DATABASE_URL)" \
		../adapters-storage-py/astrocyte-postgres/scripts/migrate.sh; \
	actual=$$(PGPASSWORD=$(BENCH_PG_PASSWORD) psql "$(BENCH_FINANCE_DATABASE_URL)" -Atc "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a WHERE a.attrelid = 'astrocyte_vectors'::regclass AND a.attname = 'embedding'"); \
	if [ "$$actual" != "vector($(BENCH_EMBEDDING_DIMENSIONS))" ]; then \
		echo "  [bench-finance-db] ERROR: astrocyte_vectors.embedding is $$actual, expected vector($(BENCH_EMBEDDING_DIMENSIONS))."; \
		echo "  [bench-finance-db] Run 'make bench-finance-db-reset' once to recreate the database."; \
		exit 1; \
	fi

bench-finance-db-stop:
	@docker stop $(BENCH_FINANCE_CONTAINER) 2>/dev/null && docker rm $(BENCH_FINANCE_CONTAINER) 2>/dev/null && \
		echo "  [bench-finance-db] $(BENCH_FINANCE_CONTAINER) stopped" || \
		echo "  [bench-finance-db] $(BENCH_FINANCE_CONTAINER) not running"

bench-finance-db-reset: bench-finance-db-stop bench-finance-db-start

bench-financebench: fetch-financebench bench-finance-db-start bench-runner-deps
	@mkdir -p $(RESULTS_DIR)/financebench/$(FINANCE_PROJECT)
	doppler run -- env DATABASE_URL='$(BENCH_FINANCE_DATABASE_URL)' ASTROCYTE_PG_DSN='$(BENCH_FINANCE_DATABASE_URL)' \
	$(BENCH_UV_RUN) python scripts/financebench/run_financebench.py \
		--dataset-dir $(FINANCE_DIR) \
		--strategy $(FINANCE_STRATEGY) \
		--project $(FINANCE_PROJECT) \
		--answerer-model $(FINANCE_ANSWERER_MODEL) \
		--judge-model $(FINANCE_JUDGE_MODEL) \
		$(if $(FINANCE_MAX_Q),--max-questions $(FINANCE_MAX_Q),) \
		--output-dir $(RESULTS_DIR)/financebench/$(FINANCE_PROJECT)

# Archive the latest FinanceBench result JSON to R2.
# Usage: make bench-archive-financebench STAGE=m19-close
bench-archive-financebench:
	@latest="$$(ls -t $(RESULTS_DIR)/financebench/$(FINANCE_PROJECT)/financebench_results_*.json 2>/dev/null | head -1)"; \
	if [ -z "$$latest" ]; then echo "no FinanceBench results in $(RESULTS_DIR)/financebench/$(FINANCE_PROJECT)/" >&2; exit 1; fi; \
	echo "archiving stage=$(STAGE) project=$(FINANCE_PROJECT)"; \
	echo "  FinanceBench -> $$latest"; \
	doppler run --config bench -- $(BENCH_UV_RUN) python -m scripts.archive_bench_results \
		--stage $(STAGE) --bench financebench --files "$$latest"

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

.PHONY: clean-datasets clean-results

clean-datasets:
	rm -rf $(DATASETS_DIR)

clean-results:
	rm -rf $(RESULTS_DIR)
