# AML submission entrypoint

The documented Docker entrypoint for the Agent Memory Leaderboard's
`maintainer_wrapped` route, where maintainers *"clone the verified public
GitHub repository, build and start the documented Docker entrypoint, wrap it
as the official API, and run the evaluation."* No public HTTPS service and no
funded endpoint are required.

## Run it

From the **repository root**:

```bash
export OPENAI_API_KEY=sk-...
docker compose -f astrocyte-services-py/astrocyte-aml-py/docker-compose.yml up --build
```

Wait for the `adapter` container to report **healthy**, then evaluate against
`http://localhost:8080`.

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Readiness |
| `/add` | POST | `{request_id, session_id, user_id, messages[]}` |
| `/search` | POST | `{query, user_id, top_k, options?}` |

What is being run and why it should reproduce, the internal self-evaluation,
and attribution are in [`../SUBMISSION.md`](../SUBMISSION.md).

Set `ASTROCYTE_AML_API_KEY` to require a shared secret on every request
(`X-Api-Key`, or `Authorization: Bearer|Token`). Leave it unset for open
access. Set `AML_HOST_PORT` to publish on a different host port — 8080 is a
commonly occupied default, and a collision silently routes probes to whatever
else is listening rather than failing loudly.

## Things worth knowing before you change anything

**The build context is the repository root, not this directory.** The adapter
resolves `astrocyte` from a sibling path, so a context rooted here cannot see
the library. The compose file already sets this; a bare
`docker build .` from this directory will fail.

**`/health` builds the brain rather than returning a static `ok`.** Provider
and database misconfiguration otherwise surfaces only on the first `/add`, so
a static check would report a green service that fails on the evaluator's
first request. Unhealthy here means *"`/add` would fail"*, not *"the process
is dead"*.

**`gpt-4o-mini` is mandated, not a default.** AML requires it for both Add and
Search; the platform owns the Answer and Judge models separately, and
participants fund their own Add/Search spend. Do not switch providers for a
submission run.

**`parallel_chunks` is pinned off in `astrocyte.aml.yaml`, deliberately.** It
converts one batched extraction call into roughly one per chunk (~40 for a
2,000-word Add). Across the full suite that is approximately the difference
between \$230 and \$720 of gpt-4o-mini spend. Turn it on only if a measured
accuracy gain justifies the cost.

**`/add` is idempotent under retry.** The platform retries 408/429/500/524
up to 32 times. A retry joins an in-flight attempt or replays a recorded
success, so a batch is never ingested twice; a failure is never recorded, so
its retry genuinely re-runs. The replay memory is process-local and bounded
(`ASTROCYTE_AML_ADD_REPLAY_CAPACITY`, default 200,000 entries — a full suite
fits). After a restart a retry simply re-runs, and pipeline dedup still applies.

**`ASTROCYTE_OPENAI_MAX_CONCURRENCY` caps concurrent OpenAI calls** (0, the
default, is unbounded). Set it below your account's rate limit if the
evaluator's concurrency produces 429s.

**Versions are pinned via `ASTROCYTE_VERSION`.** `astrocyte` and
`astrocyte-postgres` derive their version from git, and the build context has
no `.git`. Pinning is also better provenance — the version the image reports
is a declared input rather than a side effect of how the repo was cloned, and
it appears in OKF exports as `generated.by: astrocyte/<version>`.

## Index type: exact search, deliberately

`bootstrap_schema: true` creates the schema with pgvector's `vector` type and
**no approximate-nearest-neighbour index**, so similarity search is an exact
cosine scan. The pgvectorscale DiskANN indexes are owned by migrations, not
bootstrap. For this workload exact search is the better choice: banks are
per-user and small, an exact scan is deterministic (which matters under the
reproduction clause), and its recall is at least an ANN index's. Every
internal self-evaluation of this configuration used the same exact scan, so
the measured and submitted setups match.

## Verified

Rebuilt and run end to end on 2026-10-04 from a clean checkout:

- image builds from a clean context; both containers report healthy; the image
  reports `astrocyte 0.17.0`
- `/add` executes the full retain path and fails **only** on OpenAI
  authentication under a dummy key; a repeat of the failed request re-runs
  rather than replaying the failure
- Postgres verified independently of OpenAI: bootstrap creates 4 tables, the
  `vector` extension, and `astrocyte_vectors.embedding vector(1536)`, with
  B-tree and full-text indexes and no ANN index

Not yet verified: a full run against a funded key.
