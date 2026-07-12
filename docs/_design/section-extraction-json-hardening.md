# Section-extraction JSON parsing hardening

## Problem

During v015f LME bench we observed 16 `JSONDecodeError` warnings spread
across 14 distinct documents (out of ~50 ingested). Both extractors —
`astrocyte/pipeline/section_fact_extraction.py` and
`astrocyte/pipeline/section_entity_extraction.py` — log a warning on
parse failure and return `[]`. The section's raw text still reaches the
section-recall path via the markdown index, but fact- and
entity-grained recall is silently disabled for that section. Estimated
LME impact: ~2–4 percentage points on aggregate accuracy.

Two contributing causes for the failures:

1. **Budget truncation.** `max_tokens=1500` (facts) and `max_tokens=500`
   (entities) are tight against the prompts' worst-case output sizes.
   gpt-4o-mini truncates the response mid-JSON when it would have
   exceeded the cap.
2. **Markdown-fence wrapping.** Despite
   `response_format={"type": "json_object"}` being set on every call,
   gpt-4o-mini occasionally returns ```` ```json ... ``` ```` (or just
   leading prose like "Sure, here you go: \{ ... \}") instead of a bare
   JSON object. `json.loads` rejects the wrapped form.

## Fix

Three layered changes, in order of cost/benefit:

1. **Raise `max_tokens` budgets** to give the model headroom for the
   common case so truncation only happens on genuinely pathological
   sections.
   - facts: 1500 → 2000
   - entities: 500 → 750
2. **Tolerant JSON parser** in `astrocyte/pipeline/_json_tolerant.py`:
   - try `json.loads` straight
   - strip ` ```json `/ ` ``` ` markdown fence wrapper
   - slice from first `{` to last `}` to drop surrounding prose
   - fall through to `None` (caller keeps the existing warn + `[]`
     fallback)
3. **One-shot retry on parse failure** with a stricter system
   reminder ("Return ONLY a valid JSON object. No markdown fences. No
   prose.") prepended to the original prompt. Capped at one retry to
   keep p95 latency stable. A sibling helper `looks_truncated()`
   short-circuits the retry when the failure was almost certainly
   budget-truncation (unbalanced braces, trailing `,`/`:`,
   non-terminator last char) — a retry under the same cap would hit
   the same wall.

The tolerant parser handles markdown-fence noise without any retry
cost; the retry handles refusal-style prose ("Sorry, I cannot do
that.") that the tolerant parser can't slice into JSON.

## Scope

Generic across benches — there is no LME- or LoCoMo-specific gating.
The hardening applies to every ingest that goes through the section
extractors.

Postgres / in-memory parity preserved — both adapters call the same
pipeline functions.

## Acceptance

Re-run a small LME slice (`per-type=5`) and confirm `JSONDecodeError`
warning count from `section_fact_extraction` and
`section_entity_extraction` drops to near zero.

## Files

- `astrocyte-py/astrocyte/pipeline/_json_tolerant.py` (new)
- `astrocyte-py/astrocyte/pipeline/section_fact_extraction.py`
- `astrocyte-py/astrocyte/pipeline/section_entity_extraction.py`
- `astrocyte-py/tests/test_json_tolerant.py` (new)
- `astrocyte-py/tests/test_section_fact_extraction.py`
- `astrocyte-py/tests/test_section_entity_extraction.py`
