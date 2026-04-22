# Benchmark snapshots

Named, hand-curated benchmark results kept in version control to document
the score progression of the Astrocyte pipeline over time. Each file is
a frozen copy of a `benchmark-results/results-*.json` run that
corresponds to a meaningful code milestone.

## Why snapshots live here (not in `benchmark-results/`)

`benchmark-results/` is gitignored — it contains ephemeral per-run
output from `scripts/run_benchmarks.py`. Every run writes new files
there. Tracking those would bloat the repo and create merge noise.

This directory (`benchmarks/snapshots/`) is **tracked**. Files here are
curated: one per benchmark per milestone. New snapshots are added by
`git add`'ing a copy from `benchmark-results/` after a meaningful
pipeline change, with a descriptive name.

## Naming convention

`{benchmark}-{version}-{shortDescription}.json`

- `{benchmark}`: `locomo` or `longmemeval`
- `{version}`: `baseline-v0`, `v1`, `v2`, … — sequential and
  monotonic. Tied to conceptual milestones, not calendar dates.
- `{shortDescription}`: optional, describes what landed at that version.

## Current snapshots (mock provider — `--provider test`)

| Version | What landed | LoCoMo overall | LongMemEval overall |
|---------|-------------|----------------|---------------------|
| v0 | Pre-RRF-improvements baseline | 60.6% | 20.0% |
| v1 | Temporal retrieval strategy   | 61.8% (+1.2pp) | 19.0% (-1pp) |
| v2 | Intent-aware recall + weighted RRF | 61.2% | 21.0% (+2pp) |

Scores are with `MockLLMProvider` + bag-of-words embeddings so
real-provider runs will differ (generally higher). Real-provider
baselines should live in a parallel file (e.g. `baselines-openai.json`)
once a CI job consistently runs against OpenAI.

## When to add a snapshot

Add a snapshot when landing a change that measurably shifts benchmark
scores by more than ~0.5pp on any category. Don't snapshot every run —
the point is to preserve a reviewable history of *why* scores changed,
not to log every execution.

## Relationship to `baselines-test-provider.json`

`../baselines-test-provider.json` (one directory up) is the **active
regression floor** — the numbers CI compares against via
`scripts/check_benchmark_regression.py`. It's updated in the same
commit that lands the improvement.

Snapshots here are **historical record** — they show the progression
but aren't consumed by automation. When a new version is promoted, the
baseline file at `benchmarks/baselines-test-provider.json` gets bumped;
the old snapshot moves to `v{N-1}-*.json` here.
