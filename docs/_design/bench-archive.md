# Benchmark result archive (Cloudflare R2)

## Why R2

Benchmark `results-*.json` files were previously written to
`benchmark-results/` (local) and `/tmp/pr*-gate-*/` (during gate runs).
Both are ephemeral: `/tmp` is wiped on reboot, and the May 2026 loss of
the high-water-mark LoCoMo result file from `/tmp/bench-m10-ab/` made
clear that we need a durable, queryable archive.

R2 is the right home:

- **Object storage, not git** — bench JSONs would otherwise pollute the
  repo with thousands of result snapshots (each ~50–500 KB
  uncompressed, ~3–30 KB gzipped).
- **S3-compatible** — `aiobotocore` works unchanged; no
  Cloudflare-specific SDK.
- **Free-tier covers our needs** — 10 GB storage + 1M Class A ops + 10M
  Class B ops per month. A year of weekly CI plus ad-hoc local runs is
  well under those limits.
- **Egress is free** — fetching trajectory data for analysis or
  rendering it in the docs site costs nothing, unlike S3.

## Two-bucket layout

R2's public-access switch is bucket-level (no per-prefix scoping). To
keep raw runs private while letting the docs site fetch a derived
trajectory artifact without authentication, the archive is split
across two buckets in the same Cloudflare account:

```
astrocyte-benchmarks                (private — API-token-only)
└── runs/<YYYY-MM-DD>/<stage>/<bench>/results-<UTC-ts>-<short-sha>.json.gz
└── runs/<YYYY-MM-DD>/manifest.json
└── latest/<bench>.json.gz

astrocyte-benchmarks-public         (public via r2.dev subdomain)
└── trajectory/locomo.json
└── trajectory/longmemeval.json
```

| Segment | Meaning |
|---|---|
| `<YYYY-MM-DD>` | UTC date the run started. One folder per day; orders chronologically when listed. |
| `<stage>` | Free-form label: `pr1-gate`, `pr2-d.5.5c-fix`, `weekly-ci`, `local-ad-hoc`. |
| `<bench>` | `locomo` or `longmemeval`. |
| `<short-sha>` | First 7 chars of the git commit the run was launched from (or `dirty` for uncommitted state). |
| `manifest.json` | Per-day index: list of all archived runs with bench, stage, sha, scores, file path. Lets `fetch` and `trajectory` tools work without paginating the whole bucket. |
| `latest/<bench>.json.gz` | Convenience pointer — copy of the most recent result for each bench. Overwritten on every archive. |
| `trajectory/<bench>.json` | Aggregated time-series derived from the private bucket's manifests; ~20 KB; the file the docs site fetches. |

All `results-*.json` are gzipped before upload (typical 15–20× size
reduction on bench output). The trajectory JSONs are uncompressed so
the browser can fetch them without a decompression step.

## Trajectory artifact (public)

`trajectory/<bench>.json` is the **only** thing the public bucket
contains. Shape:

```json
{
  "bench": "locomo",
  "updated_at": "2026-05-10T14:00:00Z",
  "runs": [
    {
      "date": "2026-05-08",
      "stage": "pr2-d55-gate",
      "sha": "1a2b3c4",
      "overall": 0.68,
      "categories": {
        "single-hop": 0.74,
        "multi-hop": 0.40,
        "temporal": 0.27,
        "open-domain": 0.81
      },
      "n_questions": 200,
      "judge": "llm",
      "result_key": "runs/2026-05-08/pr2-d55-gate/locomo/results-...json.gz"
    }
  ]
}
```

`result_key` is the **private-bucket path** to the full run JSON.
Curious readers can follow it via `fetch_bench_results.py` (which has
the credentials); browsers cannot reach it directly.

The artifact is regenerated on every archive: read all per-day
`manifest.json` files in the private bucket, project each entry into
the trajectory shape, write to the public bucket. O(N) reads on small
files; cheap enough to redo every run rather than incrementally
appending.

## Tooling

Three scripts (under `astrocyte-py/scripts/`), all async via
aiobotocore. They share one R2 client setup that reads
`R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` from
env. The token is scoped to both buckets so one client handles both.

### `archive_bench_results.py`

Uploads one or more `results-*.json` files to R2 and refreshes the
trajectory artifact.

```bash
# Manual upload (scripts/run_benchmarks.py also calls this as a post-run hook)
doppler run --config bench -- python -m scripts.archive_bench_results \
    --stage local-ad-hoc \
    --files benchmark-results/results-*.json
```

Behavior:

1. Read each result file, infer `<bench>` from the JSON's
   `benchmark_name` field.
2. gzip in-memory.
3. Upload to `R2_BUCKET` at
   `runs/<date>/<stage>/<bench>/results-<ts>-<sha>.json.gz`.
4. Update or create `runs/<date>/manifest.json` (read-modify-write
   under an `If-Match` ETag check).
5. Copy to `latest/<bench>.json.gz`.
6. Regenerate `trajectory/<bench>.json` and upload to
   `R2_BUCKET_PUBLIC`.

Flags:

- `--rebuild-trajectory` — skip steps 1–5; just rebuild the trajectory
  artifact from existing manifests. Useful if the public bucket is
  ever wiped.
- `--selftest` — verify both buckets are reachable with the configured
  token; exits 0 on success.

### `fetch_bench_results.py`

Downloads results from the private bucket for offline inspection.

```bash
# Get the latest result for one bench
doppler run --config bench -- python -m scripts.fetch_bench_results \
    --bench locomo --latest

# Get all results from a specific day
doppler run --config bench -- python -m scripts.fetch_bench_results \
    --date 2026-05-08

# Get all results from one stage across all dates
doppler run --config bench -- python -m scripts.fetch_bench_results \
    --stage pr2-d.5.5c-fix
```

Output goes to `benchmark-results/_r2/<date>/<stage>/<bench>/...` so
it lives alongside fresh local runs without colliding.

### `backfill_archive.py` (one-shot)

Walks `astrocyte-py/benchmark-results/results-*.json` and
`/tmp/pr*-gate-*/results-*.json`, infers `<stage>` from the source
location, gzips, and uploads under a `historical/` prefix in the
private bucket so the forward-going `runs/` namespace stays clean.
Updates the trajectory artifact at the end. Run once, then delete.

## Post-run hook

`scripts/run_benchmarks.py` calls `archive_bench_results` automatically
when:

- All six `R2_*` env vars are present **and**
- The run completed at least one bench successfully.

A failed upload logs a warning but does not fail the bench run — the
local `benchmark-results/` copy is the fallback. Hook is opt-out via
`BENCH_ARCHIVE_DISABLE=1`.

## Makefile targets

```bash
# Push a specific run (rarely needed; the post-run hook covers most cases)
make bench-archive-push STAGE=local-ad-hoc

# Pull latest + open in jq-friendly form
make bench-archive-fetch BENCH=locomo

# Trajectory: per-day overall + per-category for one bench (markdown table view)
make bench-archive-trajectory BENCH=locomo

# Rebuild the public trajectory artifact from scratch (after a wipe)
make bench-archive-rebuild-trajectory
```

`bench-archive-trajectory` reads the per-day `manifest.json` files,
extracts the `overall_accuracy` and `category_accuracy` fields from
each archived run, and renders a markdown table — the canonical
"how have we trended" CLI view. The docs site renders the same data
visually (see below).

## Visualization in the docs site

The docs site (Astro) fetches `trajectory/<bench>.json` directly from
the r2.dev URL at runtime — no docs rebuild needed when new bench
data lands.

```astro
---
// docs/src/components/BenchTrajectory.astro
const publicUrl = import.meta.env.R2_PUBLIC_URL;
const bench = Astro.props.bench;  // "locomo" | "longmemeval"
---
<canvas
  id={`bench-chart-${bench}`}
  data-src={`${publicUrl}/trajectory/${bench}.json`}
></canvas>
<script>
  const el = document.currentScript.previousElementSibling;
  const data = await fetch(el.dataset.src).then(r => r.json());
  // chart.js / uPlot render here
</script>
```

`R2_PUBLIC_URL` is injected at docs-build time from Doppler
(`doppler run --config bench -- pnpm build`) so the URL is not
hard-coded. CORS on the public bucket allows the cross-origin fetch
(see `benchmarks-doppler-setup.md` Step 4).

The chart page lives at `docs/_design/benchmark-trajectory.md` (or
similar) and renders one chart per bench, with optional per-stage
filtering. The render is fully client-side; the docs site has no
build-time dependency on R2.

### What gets visualized (v1)

| Chart | Y-axis | X-axis | Series |
|---|---|---|---|
| Overall accuracy over time | overall % | date | one line per bench |
| Per-category over time (LoCoMo) | category % | date | 4 lines (single/multi/temporal/open) |
| Per-category over time (LME) | category % | date | 6 lines |
| Stage comparison | overall % | stage label | bar chart, latest run per stage |

## Retention

No automatic deletion in v1. R2's free tier is generous enough that a
multi-year archive fits comfortably. If/when retention becomes
necessary, R2
[Object Lifecycle](https://developers.cloudflare.com/r2/buckets/object-lifecycles/)
rules can expire `runs/<date>/...` after N days while preserving
`latest/`. The trajectory artifact is regenerated from manifests on
every archive, so deleted-but-still-in-manifest entries simply
disappear from the chart on the next run.

## Rationale for choices considered

- **Why two buckets vs one?** R2's public-access switch is
  bucket-level — there is no per-prefix scoping. Putting the
  trajectory artifact in a separate public bucket is the simplest way
  to keep raw run JSONs private while serving the chart data without
  authentication. The alternative (a Cloudflare Worker fronting one
  bucket and gating paths) adds a moving piece for no operational win
  at this scale.
- **Why r2.dev instead of a custom domain?** No domain registered for
  the project yet. r2.dev is dev-tier (rate-limited, "for
  development") but adequate for current docs traffic. Migration to a
  custom domain is a one-line change to `R2_PUBLIC_URL`.
- **Why public read instead of presigned URLs?** Presigned URLs would
  require a credential-minting API to issue them, and the data is
  intentionally public anyway. Same outcome, more moving parts.
- **Why not S3?** Cloudflare R2 has zero egress fees, which matters
  for trajectory data fetched repeatedly by every docs page view.
  Same S3 API.
- **Why not GCS / Azure Blob?** No advantage for a single-bucket use
  case; R2 is already in the team's Cloudflare tenant.
- **Why not git LFS?** Quota cost, slower checkouts, no useful diff
  on JSON blobs.
- **Why not GitHub Actions artifacts?** 90-day retention cap and not
  queryable from local dev or the docs site.
- **Why aiobotocore vs sync boto3?** The post-run hook runs inside
  `scripts/run_benchmarks.py` which is already async; staying in the
  event loop avoids a thread-pool jump for the upload.
- **Why per-day `manifest.json` instead of one global index?**
  Bounded read-modify-write contention (only same-day runs race),
  bounded manifest size (~10–50 KB even on busy days), and the
  natural unit of "what ran today" matches the trajectory view.
- **Why regenerate `trajectory/<bench>.json` from scratch each archive
  instead of appending?** Idempotent (recoverable from any state),
  trivial to reason about, and the cost is reading ~365 small
  manifests once per archive — negligible.

## See also

- [`benchmarks-doppler-setup.md`](/plugins/benchmarks-doppler-setup/) —
  R2 bucket creation, public-access toggle, CORS, API token issuance,
  Doppler secret wiring.
- [`evaluation.md`](evaluation.md) §7.3 — local run flow, where the
  archive hook fires.
- [`benchmark-roadmap.md`](benchmark-roadmap.md) — the full PR1/PR2/PR3
  trajectory whose runs the archive captures.
