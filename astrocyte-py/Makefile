SHELL := /bin/bash

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
DATASETS_DIR  := datasets
LOCOMO_DIR    := $(DATASETS_DIR)/locomo
LONGMEMEVAL_DIR := $(DATASETS_DIR)/longmemeval
RESULTS_DIR   := benchmark-results

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

.PHONY: bench bench-full bench-locomo bench-locomo-quick bench-locomo-litellm bench-locomo-litellm-quick bench-compare bench-longmemeval bench-longmemeval-quick bench-builtin bench-smoke

# Full LoCoMo benchmark (requires OPENAI_API_KEY, ~30-60 min)
# Pass RESUME=1 to continue an interrupted run: make bench-locomo RESUME=1
bench-locomo: fetch-locomo
	uv run python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--canonical-judge \
		$(if $(RESUME),--resume,)

# Quick LoCoMo subset (50 questions, ~2-3 min)
bench-locomo-quick: fetch-locomo
	uv run python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--max-questions 50 \
		--canonical-judge

# Full LongMemEval benchmark (requires OPENAI_API_KEY, ~2-4 hrs for all 500 questions)
# Pass RESUME=1 to continue an interrupted run: make bench-longmemeval RESUME=1
bench-longmemeval: fetch-longmemeval
	uv run python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks longmemeval \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--canonical-judge \
		$(if $(RESUME),--resume,)

# Quick LongMemEval subset (100 questions, stratified across all categories, ~30-60 min)
bench-longmemeval-quick: fetch-longmemeval
	uv run python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks longmemeval \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--max-questions 100 \
		--canonical-judge

# LoCoMo via LiteLLM adapter (requires OPENAI_API_KEY + astrocyte-llm-litellm installed)
bench-locomo-litellm: fetch-locomo
	uv run python scripts/run_benchmarks.py \
		--config benchmarks/config-litellm.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data

# Quick LoCoMo via LiteLLM (50 questions)
bench-locomo-litellm-quick: fetch-locomo
	uv run python scripts/run_benchmarks.py \
		--config benchmarks/config-litellm.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--max-questions 50

# Side-by-side: built-in OpenAI vs LiteLLM adapter (50 questions each)
bench-compare: fetch-locomo
	@echo "=== Built-in OpenAI provider ==="
	uv run python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--max-questions 50 \
		--output-dir $(RESULTS_DIR)/openai
	@echo ""
	@echo "=== LiteLLM adapter ==="
	uv run python scripts/run_benchmarks.py \
		--config benchmarks/config-litellm.yaml \
		--benchmarks locomo \
		--locomo-path $(LOCOMO_DIR)/data \
		--max-questions 50 \
		--output-dir $(RESULTS_DIR)/litellm
	@echo ""
	@echo "Results: $(RESULTS_DIR)/openai/latest.json vs $(RESULTS_DIR)/litellm/latest.json"

# Built-in eval suites (basic + accuracy)
bench-builtin:
	uv run python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks builtin

# All benchmarks
bench: fetch-datasets
	uv run python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks builtin longmemeval locomo \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--locomo-path $(LOCOMO_DIR)/data

# Full canonical run — LME + LoCoMo in parallel, LLM-judge, competitor-comparable numbers.
# Requires API key (use: doppler run -- make bench-full)
# Pass RESUME=1 to continue an interrupted run: doppler run -- make bench-full RESUME=1
bench-full: fetch-datasets
	uv run python scripts/run_benchmarks.py \
		--config benchmarks/config.yaml \
		--benchmarks longmemeval locomo \
		--longmemeval-path $(LONGMEMEVAL_DIR)/data \
		--locomo-path $(LOCOMO_DIR)/data \
		--canonical-judge \
		$(if $(RESUME),--resume,)

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
