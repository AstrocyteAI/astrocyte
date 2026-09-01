# astrocyte-aml-py

Agent Memory Leaderboard (AML) **Add/Search** adapter for Astrocyte.

[AML](https://agentmemoryleaderboard.ai) is the field's only multi-institution
matched-harness evaluation of agent memory systems. Participants expose **only**
`Add` and `Search` over HTTP; the platform controls answer generation, judging,
scoring, and orchestration. That isolation is the point: it measures the memory
layer rather than the answerer model.

Cycle 1 (Aug 2026) evaluated 69 frameworks — top score **58.02**, best
open-source **45.06** — against vendor self-reports in the 90s on unmatched
harnesses. Cycle 2 opens **2026-09-20**.

## Design rules

1. **Public API only.** Everything routes through `Astrocyte.retain()` /
   `Astrocyte.recall()`. No harness-private paths — AML publishes the result,
   so the code under evaluation must be the code users run (the M32 parity
   lesson).
2. **Search returns memories, never answers.** AML forbids Search generating
   or disguising answers. `recall()` is synthesis-free by construction;
   `reflect()` is never called from this service.
3. **Add confirms searchability before HTTP 200.** A pipeline failure surfaces
   as a retryable 500 rather than a false `success: true`.
4. **`user_id` is the isolation boundary**, mapped 1:1 to `bank_id`.
   `session_id` is grouping metadata only and is never used as a search filter.

## Contract mapping

| AML | Astrocyte |
|---|---|
| `user_id` | `bank_id` (storage isolation boundary) |
| `session_id` | retain metadata (grouping only) |
| `messages[]` (role/content/timestamp) | Conversation Engine (`content_type="conversation"`) |
| earliest message `timestamp` | `occurred_at` (retain-time temporal anchoring) |
| Search `query` (+ `options`) | `recall()` query; options enrich retrieval only |
| response `content` | rendered hit: `[occurred …; recorded …] (speaker) text` |
| response `score` / `created_at` | hit score / `occurred_at` fallback `retained_at` |

### Why we return ~50 results when `top_k=100`

`top_k` is a **maximum, not a quota**. Our banked M30 evidence is that answerer
accuracy peaks near 50 candidates and degrades beyond it through context
dilution — and under AML the platform's answer model consumes exactly what we
return. Tune per cycle with `ASTROCYTE_AML_RESULT_CAP`.

## Run

```bash
uvicorn astrocyte_aml.app:app --host 0.0.0.0 --port 8080
```

| Env var | Default | Purpose |
|---|---|---|
| `ASTROCYTE_CONFIG_PATH` | — | `astrocyte.yaml` for the brain |
| `ASTROCYTE_AML_API_KEY` | unset (public) | shared secret; accepts `X-Api-Key`, `Bearer`, `Token` |
| `ASTROCYTE_AML_RESULT_CAP` | `50` | max results returned to the platform |
| `ASTROCYTE_AML_FETCH_K` | `100` | recall breadth before the cap |

Endpoints: `POST /add`, `POST /search`, `GET /health` (unauthenticated).

## Submission checklist

- [ ] Deploy publicly reachable HTTPS endpoints (participants fund their own hosting)
- [ ] Verify `/health` returns 2xx unauthenticated
- [ ] Request evaluation access → receive AML Key → run smoke test
- [ ] Confirm Add latency tolerates batches of ≤20 messages / 2,000 words
- [ ] Run full evaluation suite, then request publication review

## Tests

```bash
PYTHONPATH=. pytest tests/ -q
```

33 contract-conformance tests; no DB, LLM, or network required.
