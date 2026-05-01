# Evaluation and benchmarking

Astrocyte ships built-in tools to measure memory quality, compare providers, and monitor accuracy over time. This enables data-driven provider selection and regression detection.

---

## 1. Why evaluation matters

- Users choosing between Tier 1 (built-in pipeline + pgvector) and Tier 2 (Mystique) need objective comparison data.
- Production systems need ongoing quality monitoring - memory quality can degrade as banks grow or content patterns change.
- Provider upgrades need regression testing - did the new version of Mem0 make recall worse?

---

## 2. Evaluation API

```python
from astrocyte.eval import MemoryEvaluator

brain = Astrocyte.from_config("astrocyte.yaml")
evaluator = MemoryEvaluator(brain)

# Run a standard benchmark suite
results = await evaluator.run_suite(
    suite="basic",                       # "basic" | "longmemeval" | custom path
    bank_id="eval-test-bank",           # Dedicated test bank (created if needed)
    clean_after=True,                    # Delete test bank after evaluation
)

print(f"Precision: {results.metrics.recall_precision}, Hit rate: {results.metrics.recall_hit_rate}")
```

### 2.1 Evaluation result

```python
@dataclass
class EvalResult:
    suite: str
    provider: str
    provider_tier: str                   # "storage" or "engine"
    timestamp: datetime
    metrics: EvalMetrics
    per_query_results: list[QueryResult]
    config_snapshot: dict                # Astrocyte config at eval time

@dataclass
class EvalMetrics:
    # Recall accuracy
    recall_precision: float              # Relevant results / total results
    recall_hit_rate: float               # Queries with >=1 relevant result / total queries
    recall_mrr: float                    # Mean reciprocal rank of first relevant result
    recall_ndcg: float                   # Normalized discounted cumulative gain

    # Reflect quality (if suite includes reflect tests)
    reflect_accuracy: float | None       # LLM-judged answer correctness
    reflect_completeness: float | None   # LLM-judged answer completeness
    reflect_hallucination_rate: float | None  # LLM-judged hallucination %

    # Performance
    retain_latency_p50_ms: float
    retain_latency_p95_ms: float
    recall_latency_p50_ms: float
    recall_latency_p95_ms: float
    reflect_latency_p50_ms: float | None
    reflect_latency_p95_ms: float | None

    # Efficiency
    total_tokens_used: int               # LLM tokens consumed during this eval run
    total_duration_seconds: float

@dataclass
class QueryResult:
    query: str
    expected: list[str]                  # Expected memory texts (ground truth)
    actual: list[MemoryHit]              # Actual recall results
    relevant_found: int
    precision: float
    reciprocal_rank: float
    latency_ms: float
```

### 2.2 Per-run token tracking vs. global LLM spend

`total_tokens_used` tracks the LLM tokens consumed **within a single eval run** — the sum of all `complete()` and `embed()` calls that the pipeline makes during the suite's retain, recall, and reflect phases. This serves two purposes:

1. **Cost-efficiency comparison.** When `compare_providers()` runs the same suite against different configs, token usage is the third leg of the tradeoff alongside latency and accuracy. One provider may score higher but consume 10× more tokens.
2. **Cost regression detection.** A config change that silently doubles LLM calls shows up as a spike in `total_tokens_used` even if accuracy holds steady.

This is a **separate concern** from **global LLM spend tracking** (cumulative cost across all production calls, budget alerts, per-model breakdowns). Global spend tracking is the responsibility of your **LLM gateway or aggregator** (LiteLLM, Portkey, OpenRouter, etc.) — see `architecture.md` §5 for the boundary. Astrocyte's per-run token counter works with any `LLMProvider` implementation, including ones backed by those gateways, because it accumulates `Completion.usage` at the framework level regardless of which backend is behind the protocol.

---

## 3. Built-in test suites

### 3.1 `basic` - Quick validation

A lightweight suite (20 retain + 20 recall) that validates basic functionality:

- Retain various content types (facts, experiences, conversations)
- Recall with exact matches, semantic matches, and negative queries
- Verify tag filtering, time range filtering
- Basic reflect test (if provider supports it)

Runtime: ~30 seconds.

### 3.2 `stress` - Scale testing

Tests behavior under load:

- 1000 retains across 10 banks
- 500 recalls with varying specificity
- Measures latency degradation as bank size grows
- Tests dedup detection under bulk insert
- Measures concurrent access performance

Runtime: ~5 minutes.

### 3.3 `accuracy` - Retrieval quality

Detailed accuracy measurement:

- 50 retain + 100 recall pairs with labeled ground truth
- Tests semantic similarity (paraphrased queries)
- Tests temporal reasoning ("what happened last week")
- Tests entity-based recall ("what do we know about Calvin")
- Tests negative recall (queries with no relevant memories)
- Computes precision, MRR, NDCG

Runtime: ~2 minutes.

### 3.4 `reflect` - Synthesis quality

Tests reflect/synthesis capability:

- 20 scenarios with retained context + reflect queries
- LLM-as-judge evaluation of answer quality
- Measures accuracy, completeness, hallucination rate
- Tests with and without dispositions (if supported)

Runtime: ~3 minutes (includes LLM judge calls).

### 3.5 Custom suites

Users can define custom test suites as YAML files:

```yaml
# my-eval-suite.yaml
name: "Custom domain evaluation"
retain:
  - content: "Calvin prefers dark mode in all applications"
    tags: [preference, ui]
    fact_type: experience
  - content: "The deployment pipeline uses GitHub Actions with a 10-minute timeout"
    tags: [technical, deployment]
    fact_type: world

recall:
  - query: "What are Calvin's UI preferences?"
    expected_contains: ["dark mode"]
    tags: [preference]
  - query: "How does our deployment work?"
    expected_contains: ["GitHub Actions", "timeout"]

reflect:
  - query: "Summarize what we know about Calvin's preferences"
    expected_topics: ["dark mode", "UI"]
```

```python
results = await evaluator.run_suite(
    suite="./my-eval-suite.yaml",
    bank_id="eval-custom",
)
```

---

## 4. Provider comparison

Compare two providers side-by-side:

```python
from astrocyte.eval import compare_providers, format_comparison

results = await compare_providers(
    configs=["config-pgvector.yaml", "config-mystique.yaml"],
    suite="accuracy",
)

print(format_comparison(results))
```

```
Provider Comparison: accuracy suite
────────────────────────────────────────────────
                    pgvector (Tier 1)    Mystique (Tier 2)
Recall precision    0.72                 0.89
Recall MRR          0.65                 0.82
Recall NDCG         0.71                 0.86
Recall p50 (ms)     45                   62
Recall p95 (ms)     120                  145
Reflect accuracy    0.68 (fallback)      0.91 (native)
Reflect p50 (ms)    1200                 850
Tokens used         12,400               8,200
────────────────────────────────────────────────
```

This gives users concrete data for the Tier 1 → Tier 2 upgrade decision.

---

## 5. Running evaluations in CI

Evaluations run as a **separate GitHub Actions workflow** (`eval.yml`), not as part of the main CI pipeline. This keeps fast unit-test feedback decoupled from slower, LLM-dependent eval runs.

### 5.1 Cadence

| Cadence | Suite | Trigger | Purpose |
|---------|-------|---------|---------|
| **Nightly** | `basic` | Cron (`0 5 * * *`) | Catch regressions from data drift or provider-side changes |
| **Ad-hoc** | Any | `workflow_dispatch` (GitHub UI or `gh` CLI) | On-demand validation after config changes, provider upgrades, or before releases |

The `basic` suite (~$0.13/run on GPT-4o-class models) is cheap enough for nightly runs. Heavier suites (`accuracy`, `stress`) are intended for ad-hoc use.

### 5.2 Ad-hoc runs

Trigger from the terminal:

```bash
gh workflow run eval.yml --field suite=basic
gh workflow run eval.yml --field suite=accuracy
```

Or from the GitHub Actions UI: **Actions → Eval → Run workflow → select suite**.

### 5.3 Eval workflow design

The eval workflow:

1. Checks out the repo and installs dependencies.
2. Runs `MemoryEvaluator.run_suite()` with the selected suite.
3. Uploads `EvalResult` as a JSON artifact (for historical comparison).
4. Posts a summary table to the workflow run.

The workflow requires an `OPENAI_API_KEY` (or equivalent LLM provider credential) as a repository secret — see the repo's contributing guide for setup. Eval results are **not** gated on PR merge; they are informational. Regressions surface as workflow annotations, not merge blockers.

### 5.4 Why not per-PR?

Per-PR eval runs are feasible (~$0.13/run) but deferred for now:

- Eval suites need a real LLM backend, which means **secrets in CI** — acceptable for nightly runs on `main`, but requires careful scoping for PRs from forks.
- The `basic` suite takes ~30 seconds, which is fast, but still slower than unit tests. Keeping eval separate avoids slowing down the PR feedback loop.
- Nightly runs on `main` catch the same regressions within 24 hours.

This can be revisited if the team wants tighter feedback.

### 5.5 Regression detection

The evaluator compares current results against a baseline:

```python
@dataclass
class RegressionAlert:
    metric: str                          # e.g., "recall_precision"
    current_value: float
    baseline_value: float                # Average of last 5 runs
    delta: float                         # Absolute change
    delta_percent: float                 # Percentage change
    severity: Literal["warning", "critical"]
```

### 5.6 Astrocyte config for eval

```yaml
evaluation:
  continuous:
    enabled: true
    schedule: "0 5 * * *"               # Nightly, 5am UTC
    suite: basic
    bank_id: eval-continuous             # Dedicated bank, not production
    alert_on_regression: true
    regression_threshold: 0.05           # Alert if any metric drops >5%
    alert_hook: on_eval_regression       # Trigger event hook
```

---

## 6. CLI support

```bash
# Run a benchmark
astrocyte eval --suite basic --config astrocyte.yaml

# Compare providers
astrocyte eval compare --configs config-a.yaml config-b.yaml --suite accuracy

# Run custom suite
astrocyte eval --suite ./my-suite.yaml --config astrocyte.yaml

# Output formats
astrocyte eval --suite basic --format json > results.json
astrocyte eval --suite basic --format table
```

> **Note:** The `astrocyte eval` CLI above is specified but not yet implemented. Use `scripts/run_benchmarks.py` directly or the `make` targets described in section 7.3.

---

## 7. External benchmarks

Astrocyte includes adapters for two academic memory benchmarks that test retrieval and reasoning quality on realistic conversational data.

### 7.1 LoCoMo (ECAI 2025)

[LoCoMo](https://github.com/snap-research/locomo) tests very long-term conversational memory across four QA categories:

| Category | Tests | Question count |
|---|---|---|
| Single-hop | Direct fact recall from a single session | 282 |
| Multi-hop | Reasoning across multiple sessions | 321 |
| Open-domain | Broad knowledge questions | 96 |
| Temporal | Time-aware reasoning (ordering, dates) | 841 |

The dataset contains 10 conversations with ~200 sessions each and 1,986 total questions. The adapter (`astrocyte.eval.benchmarks.locomo`) retains each session as a conversational memory with `occurred_at` timestamps and dialogue-aware chunking.

**Scoring conventions:** Two judges are supported:

- **Stemmed token-F1** (`--canonical-judge` without a real LLM provider) — the original paper's metric. Reproducible and deterministic.
- **LLM-judge** (`--canonical-judge` with a real provider) — binary yes/no per question, matching the convention used by Mem0 (ECAI 2025), Hindsight, and MemMachine. **Required for numbers directly comparable to published competitor scores.** Automatically used when `bench-full` runs with a real provider; falls back to stemmed F1 with the mock provider so `bench-smoke` stays API-key-free.

### 7.2 LongMemEval

[LongMemEval](https://github.com/xiaowu0162/LongMemEval) (ICLR 2025) tests five long-term memory abilities across 500 questions: information extraction, multi-session reasoning, temporal reasoning, knowledge updates, and abstention. The adapter (`astrocyte.eval.benchmarks.longmemeval`) retains full session haystacks (~1,500 unique sessions) and evaluates recall + reflect against labeled QA pairs.

**Scoring:** `--canonical-judge` uses the paper's LLM-judge (one LLM call per question). Without the flag, the legacy `text_overlap_score > 0.3` scorer is used (faster, not comparable to published numbers).

### 7.3 Running benchmarks locally

Datasets are fetched automatically on first run to `datasets/` (gitignored). Results are written to `benchmark-results/`. Requires Doppler for API keys on real-provider runs.

```bash
# Smoke test — in-memory, no API key needed (~25s)
make bench-smoke

# Quick subsets (requires API key via Doppler)
doppler run -- make bench-locomo-quick       # 50 questions, ~2-3 min
doppler run -- make bench-locomo-fair        # 200 questions (20×10), ~15-20 min
doppler run -- make bench-longmemeval-quick  # 100 questions, ~30-60 min

# Full canonical run — LME + LoCoMo in parallel, LLM-judge
# Produces competitor-comparable numbers
doppler run -- make bench-full

# Resume after interruption (laptop close, kill signal, etc.)
doppler run -- make bench-full RESUME=1
```

#### LoCoMo: choosing the right tier

Three LoCoMo bench tiers cover different needs along the speed/signal trade-off:

| | **`bench-locomo-quick`** | **`bench-locomo-fair`** | **`bench-locomo`** |
|---|---|---|---|
| **Questions** | 50 | 200 (20 × 10 convos) | 1,986 (full) |
| **Wall time** | ~3 min | ~15–20 min | ~3 hrs |
| **Cost (gpt-4o-mini)** | ~$0.30 | ~$1 | ~$5–10 |
| **Sampling** | First 50 (head-slice) | First 20 from EACH conversation | All |
| **Conversation coverage** | 1 conversation | **All 10 conversations** | All 10 |
| **Per-category n** | ~10 (too small) | ~30–60 (even, balanced) | ~400 |
| **95% CI on overall** | ±14 pts | ±7 pts | ±2.2 pts |
| **Comparable across runs** | ✅ deterministic | ✅ deterministic | ✅ |
| **Detects 4-pt change** | ❌ | marginal | ✅ |
| **Detects 8-pt change** | ❌ | ✅ | ✅ |

**When to use which:**

- **`quick`** — sanity check / smoke test only. 50 questions can't tell you anything statistically meaningful about quality. Use for: "does my code crash on real bench data?" and CI gates.
- **`fair`** — recommended fast-iteration target. Same speed as a 200-question head-slice but every conversation is represented, so persona scoping and cross-conversation tag filters all get exercised. Per-category numbers are reliable enough to detect ~8-pt swings.
- **`bench-locomo`** — release-quality measurement. Tight per-category CIs (±5 pts) let you make claims like "multi-hop +5 pts." Direct comparison to published numbers (Hindsight, BEAM, paper baselines).

**Recommended workflow** for each change you want to ship:

```text
1. Implement the change
2. make bench-locomo-fair       (~20 min)  → does it move the needle?
3. If no signal → reject, iterate, or shelve
4. If positive signal → make bench-locomo  (~3 hrs) → confirm magnitude
5. If confirmed at full scale → ship
```

Cost per feature: ~3.5 hrs of bench time, ~$6–11. Compare to "always full bench" at 6 hrs and ~$10–20 per feature.

##### Why no fixed 200-question head-slice tier?

A head-slice (`--max-questions 200`) draws all 200 questions from the first one or two conversations in the dataset. Persona scoping, cross-conversation tag filters, and entity disambiguation across storylines all go untested at that sample. `bench-locomo-fair` (`--max-questions-per-conversation 20`) costs the same but exercises every conversation. The deprecated `bench-locomo-200` target was removed for this reason; use `bench-locomo-fair` instead.

**Key CLI flags** (`scripts/run_benchmarks.py`):

| Flag | Effect |
|---|---|
| `--canonical-judge` | Use each benchmark's paper-specified judge. Required for competitor comparisons. |
| `--multi-query` | Enable multi-query expansion in retrieval (extra LLM calls, improves multi-hop recall). |
| `--max-sessions N` | Cap LongMemEval retain phase at N unique sessions (default: all ~1,500). |
| `--resume` | Continue an interrupted run from `benchmark-results/checkpoints/`. |
| `--max-questions N` | Hard cap on total questions (deterministic head-slice; biased toward early conversations when small). |
| `--max-questions-per-conversation N` | LoCoMo only: take the first N questions from EACH conversation. Preserves per-conversation balance. Used by `bench-locomo-fair`. |

**Checkpoint / resume:** Every evaluated question is checkpointed to `benchmark-results/checkpoints/`. If a run is interrupted, `--resume` (or `RESUME=1` in `make bench-full`) replays already-scored questions from cache and skips already-retained sessions (with persistent stores). The checkpoint is deleted on successful completion.

**Parallel execution:** When both `longmemeval` and `locomo` are requested (e.g. `bench-full`), they run concurrently via `asyncio.gather()`. Each uses its own bank ID so there is no state conflict.

### 7.4 CI integration

The GitHub Actions workflow (`.github/workflows/benchmarks.yml`) runs benchmarks weekly and on manual dispatch. It uses `--canonical-judge` and compares results against `benchmarks/baselines-openai.json` (falling back to `baselines-test-provider.json` if no real-provider baseline exists yet). The `bench-smoke` job runs on every PR using the mock provider and `baselines-test-provider.json`.

The regression gate (`scripts/check_benchmark_regression.py`) exits non-zero when any metric drops more than a configurable tolerance (default: 2pp overall, 3pp per category, 3pp retrieval metrics).

---

## 8. Evaluation and the two-tier model

| Suite | Tier 1 behavior | Tier 2 behavior |
|---|---|---|
| `basic` | Tests built-in pipeline + storage | Tests engine directly |
| `accuracy` | Measures pipeline recall quality | Measures engine recall quality |
| `reflect` | Tests fallback LLM synthesis | Tests native engine reflect |
| `stress` | Tests pipeline + storage under load | Tests engine under load |

The evaluation framework treats both tiers identically - it uses the public API (`retain`, `recall`, `reflect`). This ensures apples-to-apples comparison between tiers.
