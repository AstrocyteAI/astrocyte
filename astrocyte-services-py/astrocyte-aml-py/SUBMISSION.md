# Astrocyte — AML open-source submission materials

Reproducibility, configuration, and attribution for evaluating
[Astrocyte](../../README.md) on the Agent Memory Leaderboard's open-source
track via the `maintainer_wrapped` route. To run it, see
[`deploy/README.md`](deploy/README.md); this document covers what is being run
and why it should reproduce.

## What is evaluated

| | |
|---|---|
| System | Astrocyte, open-source memory framework (Apache-2.0) |
| Entrypoint | `docker compose -f astrocyte-services-py/astrocyte-aml-py/docker-compose.yml up --build`, from the repository root |
| Service | Add/Search adapter on `:8080` (`astrocyte_aml.app`) |
| Configuration | [`deploy/astrocyte.aml.yaml`](deploy/astrocyte.aml.yaml), baked into the image |
| Add/Search model | `gpt-4o-mini` (completion), `text-embedding-3-small` (embedding), as the rules require |
| Storage | PostgreSQL with the `vector` extension, schema bootstrapped on first use |
| Version | reported by the image as `astrocyte <ASTROCYTE_VERSION>` (Dockerfile `ARG`) |

Evaluate a **tagged release**, not a moving branch: the version the image
reports is pinned to the tag it is built from.

## How Add and Search work

Every request goes through the library's public `retain()` / `recall()` API.
The adapter adds no evaluation-specific logic, and nothing in it branches on
benchmark, dataset, or question type.

**Add** (`POST /add`) renders the message batch as a conversation, with each
turn's timestamp inlined, and calls `retain()` with `bank_id = user_id`. Retain
splits the batch into chunks and stores **each chunk's text verbatim**. One
batched `gpt-4o-mini` call returns per-chunk metadata only: when, where, who,
fact type, occurrence interval, and entities. That metadata is joined back by
an echoed `chunk_index`, so a skipped or reordered entry cannot be attached to
the wrong chunk. Chunks are embedded and written. Observation consolidation
then runs in the background, bounded with backpressure. Add returns 200 only
after the chunks are persisted and searchable.

**Search** (`POST /search`) calls `recall()` scoped to `bank_id = user_id`. It
fuses semantic, keyword (Postgres full-text), and temporal (recency)
retrieval, plus consolidated observations when the query's intent calls for
them, using weighted reciprocal-rank fusion. It then reranks and returns at
most 50 items, each a
stored memory with its occurrence date inlined. Search never generates an
answer: `recall()` makes no synthesis call, and the adapter never calls
`reflect()`. For choice questions, the options are appended to the
*retrieval* query only.

**Isolation.** `user_id` maps one-to-one to a memory bank, and every read and
write is bank-scoped at the storage layer. `session_id` is stored as metadata
and is never used as a filter, per the contract.

## Why it should reproduce

The platform reproduces submissions and may invalidate a materially different
score. These are the sources of run-to-run variance we removed:

- **Idempotent Add under retry.** The platform retries 408/429/500/524 up to
  32 times. A retry of an Add that is still running joins that attempt, and a
  retry of one that already succeeded replays the success. Neither ingests the
  batch twice. Failures are never cached, so a retry after a 500 genuinely
  re-runs. Keyed on `user_id`, `request_id`, and a digest of the content.
- **Temperature 0** on every extraction call.
- **Exact vector search.** The bootstrapped schema has no approximate-NN index,
  so similarity search is an exact cosine scan. That is deterministic, and its
  recall is at least as good as an ANN index's. Banks are per-user, so the scan
  stays small.
- **No silent degradation.** `escalation.degraded_mode: error` turns a failed
  pipeline stage into a 500 the platform retries, rather than a partial memory
  stored as success.
- **No deletion mid-run.** Lifecycle management is off.

Residual nondeterminism we cannot remove: `gpt-4o-mini` output is not
bit-identical at temperature 0, and background consolidation interleaves
differently under different request concurrency.

## Resources and cost

Participants fund their own Add/Search spend. Our estimate for the full textual
suite is **about $230 of `gpt-4o-mini`**, with a sensitivity range of $100–$400
depending on Adds per history. The basis is in the project roadmap (§4d).
Embedding cost is negligible. The adapter runs as a single process; Postgres is
the only other service.

If the evaluator's Add concurrency triggers OpenAI rate limits, set
`ASTROCYTE_OPENAI_MAX_CONCURRENCY` below the account's limit. A bounded queue
is cheaper than SDK retry backoff.

## Internal self-evaluation

**These are not AML scores, and they are not comparable to leaderboard
entries.** They come from our own harness (`aml_selfeval`), which uses the
public AML answer and scoring code but a different answer/judge model, a
single benchmark (LongMemEval-S), and a sample rather than the full suite.

| Run | Retain architecture | Models (Add/Search) | n | Accuracy |
|---|---|---|---|---|
| Baseline | in-memory store, structured extraction off | Claude Haiku via `claude -p`, bge-small | 250 | 59.2–60.0% (two judge passes), 95% CI [53.8, 65.9] |
| Submission architecture | this configuration, models swapped | Claude Haiku via `claude -p`, bge-small | 50, paired | +4.0 pts vs baseline on identical items, 95% CI [−4.0, +13.0], not significant |

A run with the exact submission models (`gpt-4o-mini` + `text-embedding-3-small`)
is pending.

### Reproducing the self-evaluation

The sample is LongMemEval-S shuffled with seed 42, so any prefix is a valid
random sample. With the adapter running on `:8080`:

```bash
python -m aml_selfeval.retrieve retrieve --dataset longmemeval \
  --source <longmemeval_s_shuffled_seed42.json> --output lme.jsonl \
  --base-url http://127.0.0.1:8080 --concurrency 4 --limit 250 --resume
python -m aml_selfeval.judge --aml-repo <agent-memory-leaderboard checkout> \
  --bench longmemeval-s --input lme.jsonl --output answers.jsonl --scored scored.jsonl \
  --answer-base <OpenAI-compatible base URL> --judge-base <OpenAI-compatible base URL>
```

Judge noise is material at this sample size: two independent judge passes on
the same retrievals differed by up to 4 points at n=50. Report both passes.

## Attribution

- **Agent Memory Leaderboard** — the evaluation protocol, Add/Search contract,
  and the public answer/scoring code the self-evaluation calls into.
  <https://agentmemoryleaderboard.ai/>
- **LongMemEval** (Wu et al., 2024) — the benchmark used for self-evaluation.
  Subject to its upstream license.
- **pgvector** (PostgreSQL License) — vector type and exact cosine search.
- **pgvectorscale** (PostgreSQL License) — included in the database image; not
  used by this configuration's bootstrapped schema.
- **FastAPI** (MIT), **Uvicorn** (BSD-3-Clause), **psycopg** (LGPL-3.0),
  **OpenAI Python SDK** (Apache-2.0) — runtime dependencies.
- **BAAI bge-small-en-v1.5** (MIT) — local embedding model used only in the
  self-evaluation, never in the submission.

Astrocyte itself is released under the Apache License 2.0.
