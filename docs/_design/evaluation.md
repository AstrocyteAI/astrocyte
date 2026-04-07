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

print(f"Precision: {results.metrics.precision}, Hit rate: {results.metrics.hit_rate}")
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
    total_tokens_used: int               # LLM tokens consumed during eval
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

## 5. Continuous quality monitoring

Run evaluations on a schedule against production banks (using a dedicated test partition):

```yaml
evaluation:
  continuous:
    enabled: true
    schedule: "0 5 * * 1"               # Weekly, Monday 5am
    suite: basic
    bank_id: eval-continuous             # Dedicated bank, not production
    alert_on_regression: true
    regression_threshold: 0.05           # Alert if any metric drops >5%
    alert_hook: on_eval_regression       # Trigger event hook
```

### 5.1 Regression detection

The evaluator compares current results against the last N runs:

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

---

## 7. Evaluation and the two-tier model

| Suite | Tier 1 behavior | Tier 2 behavior |
|---|---|---|
| `basic` | Tests built-in pipeline + storage | Tests engine directly |
| `accuracy` | Measures pipeline recall quality | Measures engine recall quality |
| `reflect` | Tests fallback LLM synthesis | Tests native engine reflect |
| `stress` | Tests pipeline + storage under load | Tests engine under load |

The evaluation framework treats both tiers identically - it uses the public API (`retain`, `recall`, `reflect`). This ensures apples-to-apples comparison between tiers.
