---
title: "Claude-native ablation — zero-OpenAI pipeline on LME"
draft: false
topic: design
---

# Claude-native ablation — zero-OpenAI pipeline on LME

**Status:** CLOSED — measured 2026-09-03, cycle `claude-native-r3`
**Verdict:** the stack works end-to-end and scores 83.3% LME @ n=48, but the
result is **NOT comparable to the shipped 74.4% baseline** and does not enter
`BENCH_PARITY.yaml`.

---

## 1. Why this exists

The OpenAI account ran out of credits mid-cycle. Rather than stop bench work,
we built a fully Claude-native path: every LLM call through the local Claude
Code CLI on subscription auth, embeddings through a local sentence-transformers
model. This doc records what that measured, and — more importantly — what it
does and does not license us to claim.

## 2. Configuration

| Component | Baseline (`v015w`) | This ablation (`claude-native-r3`) |
|---|---|---|
| Answerer | gpt-4o-mini | **haiku** (Claude Code CLI, subscription auth) |
| Judge | gpt-4o-mini | **haiku** |
| Pipeline LLM (tree build, summaries, extraction) | gpt-4o-mini | **haiku** |
| Embeddings | `text-embedding-3-small` (1536d) | **bge-small-en-v1.5**, local, zero-padded to 1536 |
| OpenAI calls | all | **zero** |
| Concurrency | HTTP/2 pooled | 4 CLI subprocesses (both sides) |

Invocation is documented in `astrocyte-py/Makefile` under the bench-provider
block. Providers: `astrocyte/providers/{claude_cli,local_embeddings,composite}.py`.

## 3. Result — LME, n=48 (per-type 8)

| Cutoff | Score |
|---|---|
| max_tokens_1024 | 40/48 = 83.3% |
| max_tokens_2048 | 42/48 = 87.5% |
| max_tokens_4096 | 41/48 = 85.4% |
| **max_tokens_8192** (ship-floor) | **40/48 = 83.3%** |

Per-type @ mt_8192, against the `v015b` per-category reference:

| Category | v015b baseline | r3 | Δ |
|---|---|---|---|
| knowledge-update | ~83% | 100% (8/8) | ▲ |
| single-session-assistant | 95% | 100% (8/8) | ▲ |
| multi-session | ~45% | 87.5% (7/8) | ▲▲ |
| single-session-preference | ~70% | 75% (6/8) | ~ |
| single-session-user | ~57% | 75% (6/8) | ▲ |
| **temporal-reasoning** | ~67% | **62.5% (5/8)** | ▼ |

Operational: ~20h wall clock, **zero CLI hard failures, zero circuit-breaker
trips**.

## 4. Cross-vendor judge check

To test whether a Haiku judge was simply lenient, all 48 generated answers were
re-graded by **gpt-5.5 via Codex CLI**, using the harness's `JUDGE_PROMPT`
verbatim (tool: `astrocyte-py/scripts/cross_judge_codex.py`).

| | Haiku judge | gpt-5.5 judge |
|---|---|---|
| PASS | 40/48 = 83.3% | 44/48 = 91.7% |

**Agreement 91.7%; all four disagreements favoured gpt-5.5.** An independent
vendor's frontier model was *more* generous than Haiku. The judge-leniency
hypothesis is falsified in the testable direction.

Notably both judges scored temporal-reasoning identically (5/8) — the one
category that regressed is a real weakness, not judge noise. Consistent with
date arithmetic depending on temporal resolution at retain time.

## 5. Why this is NOT a parity row

`release_mark.py` defines a parity row as *"released package version X embeds
the behavior of bench cycle Y"*, and the README badge writer publishes the
newest row as the headline score. Neither holds here:

1. **No release ships this.** The claude-native path is opt-in via env vars;
   the default provider is still OpenAI.
2. **Four variables changed at once** — answerer, judge, embedder, concurrency.
   Three of them plausibly favour the new number for reasons unrelated to
   memory quality.
3. **The answerer confound is untested and large.** The `m13.1` data point
   (LME 76.7% with a gpt-4o answerer vs ~60% with gpt-4o-mini on an unchanged
   retrieval stack) suggests answerer strength alone can move LME ~16pp.

Publishing 83.3% next to 74.4% would be the harness-mismatch error that
`benchmark-comparison-methodology.md` §5 rule 1 explicitly forbids.

## 6. The experiment that would settle it

**r4: r3's memories + gpt-4o-mini answerer and judge.** Ingest already exists
in the bench DB, so this is answer+judge only (~4h, ~$12 of OpenAI credits).
Everything then matches the baseline except the memory pipeline, making any
delta attributable to memory quality.

Blocked on OpenAI credits. `gpt-4o-mini` is unreachable by any local route:
Codex CLI serves only `gpt-5.5` on a ChatGPT account (all other model ids
rejected), and Ollama has no OpenAI models.

## 7. Artifacts

- Results (gitignored): `astrocyte-py/benchmark-results/mem0_harness/lme/claude-native-r3/`
- Run log: `astrocyte-py/benchmark-results/parallel/claude-native-r3.log`
- Per-question cross-judge verdicts: `/tmp/cross_judge_r3_final.json`

**Not archived to R2.** The mem0_harness result shape is not what
`archive_bench_results.py` classifies (it keys on a top-level bench name;
mem0_harness emits `metadata` / `metrics_by_cutoff` / `evaluations`). A first
archive attempt mislabelled unrelated `v016h` LoCoMo and LME rows under this
stage; those were purged and the trajectory rebuilt. **Open gap:** the archiver
needs a mem0_harness converter before these runs can be archived.

## 8. What this does license

- The claude-native stack is a **working, zero-cost bench path** when OpenAI
  credits are unavailable — validated over ~20h with no failures.
- `llm_provider: claude_cli` + `embedding_provider: local_embeddings` is a
  legitimate deployment configuration for users without API budget.
- Nothing about relative memory quality versus the OpenAI pipeline.
