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

Astrocyte uses project `astrocyte` with environment `bench` as the
benchmark-secrets home (separate from `prd` so production credentials
never load into bench processes).

```bash
# One-time: install Doppler CLI and authenticate
brew install dopplerhq/cli/doppler
doppler login
cd ~/AstrocyteAI/astrocyte
doppler setup --project astrocyte --config bench
```

Required secrets in the `bench` config:

| Name | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI credential used by `benchmarks/config.yaml`'s `${OPENAI_API_KEY}` substitution for both completion (`gpt-4o-mini`) and embedding (`text-embedding-3-small`) calls. |
| `R2_ACCOUNT_ID` | Cloudflare account ID; the S3 endpoint is derived as `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`. |
| `R2_ACCESS_KEY_ID` | R2 API token's access key (scoped read/write to **both** buckets below). |
| `R2_SECRET_ACCESS_KEY` | R2 API token's secret. |
| `R2_BUCKET` | Private bucket holding raw run JSONs. Astrocyte's canonical value: `astrocyte-benchmarks`. |
| `R2_BUCKET_PUBLIC` | Public bucket holding the aggregated trajectory artifact the docs site reads. Canonical value: `astrocyte-benchmarks-public`. |
| `R2_PUBLIC_URL` | The r2.dev URL Cloudflare assigns when public access is enabled on the public bucket, e.g. `https://pub-<hash>.r2.dev`. Read by the Astro docs build to wire the trajectory chart. |

The six `R2_*` secrets back the benchmark archive. The private bucket
holds every completed run's `results-*.json` so trajectory data
survives beyond `/tmp` and isn't checked into git. The public bucket
holds a small derived `trajectory/<bench>.json` artifact (~20 KB per
bench) the docs site fetches over the r2.dev URL for the time-series
chart. Raw per-question detail stays private. See
[`bench-archive.md`](/design/bench-archive/) for the full layout and
the archive/fetch tooling.

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

## R2 bucket setup

One-time setup before the `R2_*` secrets above can be filled in.

### Step 1 — Create both buckets

Cloudflare dashboard → R2 Object Storage → *Create bucket*. Repeat for
each of the two:

| Bucket | Public access | Purpose |
|---|---|---|
| `astrocyte-benchmarks` | **No** (default; token-only) | Raw `results-*.json.gz` runs, manifests, `latest/`. |
| `astrocyte-benchmarks-public` | **Yes** (r2.dev subdomain — see Step 3) | Derived `trajectory/<bench>.json` artifact for the docs site. |

Use the same location hint (*Automatic* or a specific region) for both
so cross-bucket copies stay cheap.

### Step 2 — Note the Account ID

Top-right of the R2 page; this is `R2_ACCOUNT_ID`. Both buckets share
it.

### Step 3 — Enable public access on the public bucket only

Open `astrocyte-benchmarks-public` → *Settings* → *Public access* →
*R2.dev subdomain* → **Allow Access**. Cloudflare returns a URL like:

```
https://pub-7a3f9b2c1d4e8f6a.r2.dev
```

That is `R2_PUBLIC_URL`. Files at `trajectory/locomo.json` become
reachable at:

```
https://pub-7a3f9b2c1d4e8f6a.r2.dev/trajectory/locomo.json
```

> **Caveat — r2.dev is dev-tier.** Cloudflare rate-limits the r2.dev
> subdomain and labels it "for development." For Astrocyte's docs
> traffic at current scale this is fine; the trajectory JSONs are
> small and read-only. The escape hatch when traffic outgrows it is to
> point a custom domain (e.g. `bench.astrocyte.dev`) at the public
> bucket and update `R2_PUBLIC_URL` — no code changes needed.

Do **not** enable public access on the private bucket. Run JSONs
contain per-question detail and stay token-gated.

### Step 4 — Configure CORS on the public bucket

The docs site fetches `trajectory/<bench>.json` from the browser
cross-origin, so the public bucket needs CORS. Public bucket →
*Settings* → *CORS Policy*:

```json
[
  {
    "AllowedOrigins": ["*"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["*"],
    "MaxAgeSeconds": 3600
  }
]
```

`AllowedOrigins: ["*"]` is acceptable because the trajectory data is
intentionally public. Tighten to the docs site's stable origin once
deployed.

### Step 5 — Mint one API token scoped to both buckets

R2 page → *Manage R2 API Tokens* → *Create API Token*:

- Name: `astrocyte-benchmarks-rw`
- Permissions: **Object Read & Write**
- Specify bucket: **Apply to specific buckets only** → check both
  `astrocyte-benchmarks` and `astrocyte-benchmarks-public`.
- TTL: blank (or 1 year).

Copy the **Access Key ID** and **Secret Access Key** shown once —
these are `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`. One token
serves both buckets so the archive script doesn't juggle credentials.

### Step 6 — Store in Doppler

```bash
doppler secrets set R2_ACCOUNT_ID --config bench
doppler secrets set R2_ACCESS_KEY_ID --config bench
doppler secrets set R2_SECRET_ACCESS_KEY --config bench
doppler secrets set R2_BUCKET --config bench         # value: astrocyte-benchmarks
doppler secrets set R2_BUCKET_PUBLIC --config bench  # value: astrocyte-benchmarks-public
doppler secrets set R2_PUBLIC_URL --config bench     # value: https://pub-<hash>.r2.dev
```

### Step 7 — Verify

```bash
doppler run --config bench -- env | grep ^R2_   # six values present
doppler run --config bench -- python -m scripts.archive_bench_results --selftest
curl -fsS "$(doppler secrets get R2_PUBLIC_URL --plain --config bench)/trajectory/locomo.json" | head -c 200
```

The `curl` returns 404 until the first run is archived; the goal is
just to confirm the public URL resolves and CORS doesn't reject the
request shape.

Astrocyte uses the **S3-compatible** R2 API. `aiobotocore` (async
`boto3`) is the dependency; `region_name="auto"` is the correct value
for R2 (do not pass a real AWS region).

## CI setup

The weekly benchmark workflow at `.github/workflows/benchmarks.yml` uses
a **Doppler service token** scoped to the `bench` config so the same
secret rotation flow applies. The CI run uploads results to R2 via the
same `R2_*` secrets.

### One-time setup in Doppler

Create a service token scoped to read-only for the `bench` config:

```bash
doppler configs tokens create benchmark-ci --config bench --plain
# Copy the token output
```

### One-time setup in GitHub

Add the token as a repository secret:

1. GitHub → Settings → Secrets and variables → Actions → New repository secret
2. Name: `DOPPLER_BENCHMARK_TOKEN`
3. Value: the token from the Doppler command above

The workflow already reads `secrets.DOPPLER_BENCHMARK_TOKEN` — no
other per-provider secrets are needed. The Doppler CLI step injects
`OPENAI_API_KEY`, the four `R2_*` secrets, and any other `bench`
secrets into the benchmark process's environment.

### Rotation

Rotate via Doppler:

```bash
# Revoke compromised token
doppler configs tokens revoke <token-slug>

# Issue replacement
doppler configs tokens create benchmark-ci --config bench --plain
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
exactly `OPENAI_API_KEY` in the `bench` config (the benchmark config's
YAML does `api_key: ${OPENAI_API_KEY}` — case-sensitive).
`doppler run --config bench -- env | grep OPENAI` should show the key
in the injected environment.

**Archive upload fails with `InvalidAccessKeyId` / `SignatureDoesNotMatch`** —
Re-issue the R2 API token (Cloudflare dashboard → R2 → *Manage R2 API
Tokens*) and re-run `doppler secrets set R2_ACCESS_KEY_ID` /
`R2_SECRET_ACCESS_KEY`. R2 tokens are bucket-scoped; if the token's
bucket doesn't match `R2_BUCKET`, signing fails with the same error.

**Archive upload fails with `NoSuchBucket`** — Verify
`doppler secrets get R2_BUCKET --plain` exactly equals
`astrocyte-benchmarks` (case-sensitive; no `s3://` prefix, no path
suffix).
