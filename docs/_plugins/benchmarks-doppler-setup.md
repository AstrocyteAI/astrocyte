---
title: Benchmark provider secrets — Doppler setup
description: How to run Astrocyte benchmarks against real LLM providers via Doppler both locally and in CI.
---

# Benchmark provider secrets via Doppler

Astrocyte benchmarks run two modes:

- **Mock provider** (`--provider test`) — no API key needed. Fast, deterministic,
  used by the per-PR regression gate against
  `benchmarks/baselines-test-provider.json`.
- **Real provider** (`--config benchmarks/config.yaml`) — uses OpenAI for
  LLM + embeddings. Produces the numbers that inform positioning and that
  users actually care about.

Doppler is the source of truth for real-provider secrets both locally and
in CI. This guide covers the one-time setup.

## Local developer setup

Astrocyte uses project `astrocyte` with environment `prd` as the
benchmark-secrets home.

```bash
# One-time: install Doppler CLI and authenticate
brew install dopplerhq/cli/doppler
doppler login
cd ~/AstrocyteAI/astrocyte
doppler setup --project astrocyte --config prd
```

Required secret in the `prd` config:

| Name | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI credential used by `benchmarks/config.yaml`'s `${OPENAI_API_KEY}` substitution for both completion (`gpt-4o-mini`) and embedding (`text-embedding-3-small`) calls. |

Run any benchmark command with `doppler run --` as the prefix:

```bash
cd astrocyte-py

# LoCoMo smoke test — 20 questions, ~30¢
doppler run -- uv run python scripts/run_benchmarks.py \
    --config benchmarks/config.yaml \
    --benchmarks locomo \
    --locomo-path datasets/locomo/data/locomo10.json \
    --max-questions 20

# LongMemEval — pricier per question (longer contexts)
doppler run -- uv run python scripts/run_benchmarks.py \
    --config benchmarks/config.yaml \
    --benchmarks longmemeval \
    --longmemeval-path datasets/longmemeval/data/longmemeval_s_cleaned.json \
    --max-questions 100
```

Output writes to `benchmark-results/`. To promote a run to a tracked
snapshot:

```bash
cp benchmark-results/results-$(date -u +%Y%m%dT%H%M%SZ | cut -c1-15)*.json \
   benchmarks/snapshots/locomo-v3-real-openai.json
git add benchmarks/snapshots/locomo-v3-real-openai.json
```

See [`benchmarks/snapshots/README.md`](../../astrocyte-py/benchmarks/snapshots/README.md)
for the naming convention.

## CI setup

The weekly benchmark workflow at `.github/workflows/benchmarks.yml` uses
a **Doppler service token** scoped to the `prd` config so the same
secret rotation flow applies.

### One-time setup in Doppler

Create a service token scoped to read-only for the `prd` config:

```bash
doppler configs tokens create benchmark-ci --config prd --plain
# Copy the token output
```

### One-time setup in GitHub

Add the token as a repository secret:

1. GitHub → Settings → Secrets and variables → Actions → New repository secret
2. Name: `DOPPLER_BENCHMARK_TOKEN`
3. Value: the token from the Doppler command above

The workflow already reads `secrets.DOPPLER_BENCHMARK_TOKEN` — no
other per-provider secrets are needed. The Doppler CLI step injects
`OPENAI_API_KEY` and any other `prd` secrets into the benchmark
process's environment.

### Rotation

Rotate via Doppler:

```bash
# Revoke compromised token
doppler configs tokens revoke <token-slug>

# Issue replacement
doppler configs tokens create benchmark-ci --config prd --plain
```

Update the `DOPPLER_BENCHMARK_TOKEN` secret in GitHub and the next
workflow run picks it up. No direct handling of `OPENAI_API_KEY` in CI
logs or workflow files.

## Budget hygiene

Real-provider runs consume LLM credit; keep them under control:

| Scenario | Cost estimate |
|---|---|
| LoCoMo 200Q, `gpt-4o-mini` | ~$0.60 |
| LoCoMo 1986Q (full), `gpt-4o-mini` | ~$5 |
| LongMemEval 100Q (long contexts) | ~$2 |
| LongMemEval 500Q (full) | ~$10 |

The default CI schedule is weekly Monday 06:00 UTC. `max_questions`
defaults to 200 for that run — change via workflow_dispatch if you need
a fuller sweep.

## Why not just a GitHub secret?

A single `OPENAI_API_KEY` repository secret would work, but Doppler
gives:

1. **Single rotation story** — one Doppler service token revocation cuts
   off both local developers and CI. No per-environment secret drift.
2. **Audit trail** — Doppler logs every secret fetch including CI
   workflow origin. GitHub Actions alone doesn't surface secret-access
   metrics.
3. **Environment parity** — the same secrets flow into MCP deployments,
   Mystique, and benchmarks without each invention needing its own
   secret-loading path.
4. **Project-scoped isolation** — the Astrocyte project has its own
   Doppler environment, separate from other projects the user runs.

## Troubleshooting

**`doppler: command not found`** — Install the CLI
(`brew install dopplerhq/cli/doppler` on macOS) and run `doppler login`.

**`Error: no token provided`** — Local: run `doppler setup` to bind the
project/config. CI: confirm `DOPPLER_BENCHMARK_TOKEN` is set as a
repository secret and its slug matches the workflow env block.

**Benchmark runs but LLM calls fail** — Confirm the secret name is
exactly `OPENAI_API_KEY` in the `prd` config (the benchmark config's
YAML does `api_key: ${OPENAI_API_KEY}` — case-sensitive).
`doppler run -- env | grep OPENAI` should show the key in the injected
environment.
