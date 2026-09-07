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

**Versions are pinned via `ASTROCYTE_VERSION`.** `astrocyte` and
`astrocyte-postgres` derive their version from git, and the build context has
no `.git`. Pinning is also better provenance — the version the image reports
is a declared input rather than a side effect of how the repo was cloned, and
it appears in OKF exports as `generated.by: astrocyte/<version>`.

## Known limitation: index type

`bootstrap_schema: true` creates a working schema using pgvector's `vector`
type. It does **not** create the pgvectorscale DiskANN indexes that this
project standardised on, which are owned by migrations rather than by
bootstrap. Retrieval is correct either way, but index-dependent latency will
not match the benchmark configuration. For a run where that matters, use the
migration path (`astrocyte-services-py` runbook + `config.runbook.example.yaml`)
instead of `bootstrap_schema`.

## Verified

Built and run end to end on 2026-09-07:

- image builds from a clean context; stack comes up; adapter reports healthy
- `/add` and `/search` execute the full retain/recall path and fail **only**
  on API-key authentication when given a dummy key — i.e. everything up to
  OpenAI is wired
- Postgres verified independently of OpenAI: schema bootstraps (4 tables),
  the `vector` extension is present, and `astrocyte_vectors.embedding` is
  `vector(1536)`, matching the configured embedding model

Not yet verified: a full run against a funded key, which is what the
calibration run in the roadmap (§4d) is for.
