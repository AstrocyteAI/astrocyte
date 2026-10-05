# Team memory

Status: **accepted** (October 2026; decisions in §9). G1 (gateway auth), G2 (batch push), G3 (changes feed), C1 (`astrocyte team`) and C2 (background sync, attribution, forget scopes) implemented; G4 and C3 not yet.

A developer's coding agents already remember a project locally: one SQLite file, per-project banks, automatic capture and recall. Team memory shares a project's memory with the people working on it, through an Astrocyte gateway the team runs. A decision Alice's agent saved on Monday is recalled by Bob's agent on Tuesday, attributed to her. Nothing changes on the hot path: hooks and the MCP server still read and write only the local store.

For the local install see the end-user quick start; for access control, `access-control.md`; for multi-bank recall, `multi-bank-orchestration.md`; for the existing replication idea this replaces for the local case, `memory-export-sink.md`.

---

## 1. Goals and non-goals

**Goals**

- Share a project's durable knowledge (decisions, conventions, imported docs) across a team's agents: Claude Code, Codex, Antigravity, Copilot CLI.
- Recall stays local and fast (the prompt hook's budget is 1.5 s; today it takes ~10 ms). It works offline; sync catches up later.
- Every shared memory says who shared it, and an erased memory is erased for everyone, from disk.
- Self-hosted on the existing gateway. No new service.

**Non-goals (v1)**

- Real-time collaboration. Minutes of lag are fine.
- Sharing conversations by default (see §2).
- Resolving contradictions between teammates' memories. That is memory quality (recency, supersession), not sync.
- Cross-organization federation; a hosted offering.

## 2. What syncs

The unit of sharing is a **project bank**. A bank's id is already derived from the normalised git `origin` URL (`astrocyte/harness/project.py`), so every teammate's clone lands on the same `project:<name>-<hash>`. The team bank on the gateway uses the same id, so no mapping table is needed.

Within a joined project, what is shared depends on how the memory was made:

| Memory | Shared? | Why |
|---|---|---|
| Saved by an agent (`memory_retain`) | yes | A deliberate "remember this": the case team memory exists for |
| Imported (`astrocyte memory import`, `metadata.import_hash`) | yes | The project's own instructions and docs |
| Captured turns (tag `captured`) | **no**, opt-in per project | Raw conversation: half-formed ideas, personal asides, mistakes. Credentials are redacted, but the content is still a person's working session |
| Tagged `private` | never | Per-memory override (`astrocyte memory unshare <id>`) |

`astrocyte memory share <id>` promotes a single captured turn. A project can opt in to sharing captured turns (`team.share_captured: true`), but it is never the default.

**First sync is previewed.** `astrocyte team join` ends with `astrocyte team sync --dry-run`: the count, and a sample, of what would go up. The user confirms before anything leaves the machine.

## 3. Topology: replicate, don't proxy

```
 Alice's laptop                          Gateway (team)                     Bob's laptop
 ┌──────────────────────┐   push   ┌─────────────────────────┐   pull   ┌──────────────────────┐
 │ hooks / MCP          │ ───────► │ bank project:api-1a2b3c  │ ───────► │ hooks / MCP          │
 │   ↕ (local only)     │          │  access control, PII,   │          │   ↕ (local only)     │
 │ SQLite: own + mirror │ ◄─────── │  provenance, tombstones │ ◄─────── │ SQLite: own + mirror │
 └──────────────────────┘   pull   └─────────────────────────┘   push   └──────────────────────┘
        agent daemon syncs in the background (and `astrocyte team sync` on demand)
```

Teammates' memories are **pulled into the local bank** and recalled like any other memory: the same relevance gate, boot summary and resume. The agent daemon pushes and pulls every few minutes and on demand.

**Rejected: live remote recall per prompt.** The gateway already supports federated "proxy" recall (`recall/proxy.py`), but:
- it would put a network round trip on the prompt path and fail offline;
- it refuses private and loopback addresses, which is where self-hosted gateways live;
- it drops timestamps.

Replication costs disk (the team's text, re-embedded locally) and buys latency, offline use, and one recall path.

## 4. Identity and the wire format

**Memory ids are kept end to end.** Local rows (one per chunk) have random 64-bit ids (`uuid4().hex[:16]`), and both stores upsert on `id`. A pushed row keeps its id on the gateway and in every mirror, so push and pull are idempotent and a forget can name the same row everywhere. (AMA import mints new ids today, so it can't be the sync path.)

**Text, not vectors.** Each side embeds with its own model: the gateway may use 1536-dimension OpenAI embeddings, while laptops use 384-dimension bge-small. A pushed record carries:

```json
{"id": "9f2c…", "text": "…", "occurred_at": "…", "tags": ["…"], "fact_type": "…",
 "metadata": {"_created_at": "…", "session_id": "…", "source": "codex"}, "content_hash": "sha256:…"}
```

`_created_at` is kept: chunks of one turn share it, and recall regroups them by it.

**Duplicates across people.** The gateway's retain dedup already checks the store (#93). A pushed row that near-duplicates an existing team memory is answered with `duplicate_of: <id>`. The client records the mapping and doesn't push the row again. First writer wins, and both copies stay findable locally.

**Conflicts.** A memory's **text is immutable**: a push of an existing id with different text is rejected, and nobody edits a memory's text, so there are no write conflicts on it to resolve. Its provenance and status fields are not immutable: synced records will carry claim status, trust and "may be stale" flags that change after the record is saved. Those will be updatable through a separate endpoint (not push; a push with the same text and different metadata is answered `unchanged` and changes nothing), and every such change reaches mirrors through the changes feed as an upsert of the same id (§8, G3). Two teammates can still disagree ("we use SQS" vs. a later "moved to Kafka"). That is the same problem as one person changing their mind, and recall already handles it with recency (`occurred_at`) and observation supersession. Out of scope here.

## 5. Forgetting

Forgetting stays an erase, not a hide.

- **Team forget.** `astrocyte memory forget <id> --team` calls the gateway's `forget` by id (needs the `forget` permission). The gateway records a tombstone. Every mirror sees it at its next pull and **purges** the row: secure delete and VACUUM, as `astrocyte memory forget` does today.
- **Local forget of a shared memory** asks for the scope instead of guessing:

  ```
  This memory is shared with the team (saved by alice). Erase it for everyone with --team (you have forget permission),
  or hide it on this machine with --local.
  ```

  `--local` purges the row and records the id in a local suppression list, so the next pull does not bring it back.
- **Legal hold** on the gateway blocks a team forget, and the CLI reports it. Holds are in-memory only today (`lifecycle.py`), so persisting them is a prerequisite (§8, G4).
- **DSAR** (`/v1/dsar/forget_principal`) forgets by tag, which the pipeline gateway answers with 501 today. Team memory needs forget-by-actor (`metadata._actor`) to work before it ships to a team with compliance obligations (G4).

## 6. Auth and provenance

The gateway's auth is not ready for teams, and this is the first thing to fix (§8, G1):

| Today | Problem for a team | Change |
|---|---|---|
| `api_key` mode: one shared key; the principal comes from the client's `X-Astrocyte-Principal` header | Anyone with the key can act as anyone | **Per-user tokens** (`ASTROCYTE_AUTH_MODE=token`): `python -m astrocyte_gateway.tokens create --principal user:alice --banks 'project:*' --groups team:api` mints a token stored hashed and bound to a principal and its grants; the principal header is ignored. OIDC (`jwt_oidc`) remains the option for teams with an identity provider |
| Grants match a bank exactly or `*` | Can't grant `project:*` without granting everything | Prefix and glob matching in grants (`project:*`), plus a `team:<name>` group of principals |
| `_actor` is stamped with `setdefault`, so a client-supplied `_actor` wins | Provenance can be forged | The gateway always stamps `_actor` from the authenticated principal and ignores the client's |
| `/v1/import` bypasses PII, validation and `_actor` | — | The sync push path goes through the full policy layer, like `/v1/retain` |

**How a token's grants combine with config `access_grants`** (decided in G1): they are **added** for the token's principal and never remove anything. Effective permissions on a bank are the union of the principal's config grants, the config grants to its `team:` groups, and the token's own grants. So a team is usually configured once (`project:* → team:api: [read, write]`) and each token only names its person and groups; a restricted token (read-only CI) comes from keeping that principal's config grants narrow. Patterns are fnmatch, case-sensitive: `project:*` matches `project:api-1a2b3c`, not `projectx`. Detail: `access-control.md` §1.3 and §1.5.

`_actor` is stamped by the core: with a context present it overwrites the client's value; without one (library imports) a caller's `_actor` is kept, and the gateway drops it from anonymous HTTP requests.

Locally, the token is stored in the OS keychain where `keyring` is available, else in `harnesses.json`'s sibling `team.json` (0600). It is never written into agent configs.

Attribution shows in recall and boot: `- [2026-10-02] (alice) Q: … → A: …`. Your own memories carry no label. The session-start "where you left off" note considers only your own sessions.

## 7. Client surface

```
astrocyte team join https://memory.example.com --token …   # this project; previews, then confirms the first push
astrocyte team status                                       # gateway, last sync, pending push/pull, permissions
astrocyte team sync [--dry-run]                             # now, instead of waiting for the daemon
astrocyte team leave [--keep-mirror]                        # stop syncing; by default purge teammates' memories
astrocyte memory share|unshare <id>
astrocyte memory forget <id> --team|--local
```

`astrocyte doctor` adds a team section: gateway reachable, token valid, permissions per bank, last successful sync, and a backlog that isn't draining.

Sync state lives in the state directory: a push ledger (`memory_id → acked | duplicate_of | rejected`), a pull cursor per bank, and the suppression list. It does not go in the memory store, so `astrocyte memory export` stays clean.

## 8. Plan (PRs, in order)

Gateway (server side; each is useful on its own):

| PR | Content |
|---|---|
| **G1** | Per-user tokens bound to principal + grants; glob/prefix grants; `team:` groups; authoritative `_actor` stamping |
| **G2** (implemented) | `POST /v1/banks/{bank}/sync/push`: batch upsert with client ids through the policy layer; no re-chunking; per-record `stored \| duplicate_of \| rejected` |
| **G3** (implemented) | `GET /v1/banks/{bank}/changes?cursor=&limit=`: upserts and tombstones in order. Stores gain a `changed_at` column (last change to any synced field; backfilled as `max(retained_at, forgotten_at)`) and an index on `(bank_id, changed_at, id)` |
| **G4** | Persisted legal holds; forget by `_actor` (DSAR) |

**G3 as built.** `Astrocyte.list_changes(bank_id, cursor=, limit=, settle_seconds=)` over an optional `VectorStore.list_changes(bank_id, *, after, limit)`; the gateway serves it at `GET /v1/banks/{bank_id}/changes` (needs `read`; 501 when the store has no feed). Decisions the plan left open:

- **The changes feed is every change to a synced row, in `(changed_at, id)` order** — not only stores and forgets. Synced records will later carry claim status, trust and "may be stale" flags that change after the record is saved, so `changed_at` is the row's **last change to any synced field**, and a live entry carries the row's current values: a later status change reaches mirrors as an upsert of the same id. Every store sets `changed_at` on every write that changes a row: an insert takes the row's `retained_at`; an overwrite through `store_vectors` (a restore of a forgotten id, or a metadata rewrite that keeps the old `retained_at`, as the temporal-normalisation task does today) takes the time of the write; a forget takes the time of the forget. There is no other update path in the stores today; **any future one (status, trust, staleness) must bump `changed_at` too**, or mirrors never see the change.
- **Postgres backfill without a rewrite.** Migration 039 adds `changed_at` as a NULL column (catalog-only) and indexes `(bank_id, COALESCE(changed_at, GREATEST(retained_at, forgotten_at)), id COLLATE "C")`; the store reads `changed_at` through that exact expression. Rows written before the migration therefore read as `max(retained_at, forgotten_at)`, the backfill value, until their next write sets the column. An `UPDATE` backfill would write a new version of every row into the DiskANN index, and a generated column would rewrite the table under an exclusive lock and rebuild every index. SQLite adds the column and backfills it on open (local stores are small); the in-memory store tracks it and remembers tombstones beside its hard deletes.
- **Ids order byte-wise** (`COLLATE "C"` on Postgres, `BINARY` in SQLite), so the order and the cursor don't depend on the database locale, and SQLite and Postgres agree (the parity suite checks mixed-case and `_`/`-` ids).
- **Cursor.** URL-safe base64 of `{"changed_at", "id"}`, opaque to clients; one the server can't parse is a 400 (`InvalidCursor`). A page returns `next_cursor` (the position after its last change, or the request's own cursor when empty, so a caught-up client keeps its place) and `has_more`.
- **Tombstones carry nothing but `id`, `deleted` and `changed_at`**, so a forgotten memory's text never leaves the gateway again. A live entry includes `memory_layer` as well as the planned fields, so a client can tell server-side observations and mental models from teammates' memories (whether mirrors should pull those is for C1/C2).
- **Settle window.** `changed_at` is stamped just before a write commits, so with concurrent writers a slow commit could land behind a cursor that already moved past it, and that row would never be pulled. The gateway holds back changes younger than `ASTROCYTE_CHANGES_SETTLE_SECONDS` (default 5 s; the library default is 0). This assumes commits take less than the window and that the gateway hosts' clocks agree; a feed that needs a hard guarantee would order by a commit-time sequence instead.
- **Retention of tombstones.** Soft-deleted rows (and so tombstones) are kept indefinitely today; nothing purges them on the gateway. If a purge is added, a mirror whose cursor is older than the purge horizon must re-sync from scratch. The local SQLite store's `astrocyte memory forget` purges rows outright and so leaves no tombstone, which is fine: it doesn't serve a feed.

**G2 as built.** `Astrocyte.push_records(bank_id, records, context=)` runs each `SyncPushRecord` through the retain policy layer and hands the survivors to the pipeline, which stores each as exactly one row with the client's id: no chunking, no extraction, no LLM call; the server re-embeds the text. The gateway serves it at `POST /v1/banks/{bank_id}/sync/push` (needs `write`). Two new optional store methods: `lookup_ids(ids)` (the current state of each id **in any bank**, live or tombstone) and `insert_vectors(items)` (insert-only, `ON CONFLICT (id) DO NOTHING`, returns the ids inserted); Postgres, SQLite and in-memory implement both, other stores answer 501. Per record:

| Result | When |
|---|---|
| `stored` | New id; a row with exactly that id |
| `unchanged` | The id holds the same text in this bank. If metadata, tags, `fact_type` or `occurred_at` differ, they are **not** applied and `reason` says "text unchanged; metadata updates are not accepted by push" (status/provenance changes get their own endpoint) |
| `duplicate` + `duplicate_of` | A new id that near-duplicates a memory of this bank (the retain dedup check: the in-process cache, then the store's nearest neighbours, bank threshold and negation guard). Nothing stored. With `signal_quality.dedup.action: warn` it is stored instead |
| `rejected` + `reason` | The id holds other text in this bank **or exists in another bank** (one wording for both, so a push can't probe other banks; the other bank's row is never overwritten, moved or shown); the id was forgotten in this bank (a forget is not undone by a re-push); or the policy layer refused it (size, validation, PII `reject`, `content_hash` mismatch) |

Decisions the plan left open:

- **Ids are global, so the write is insert-only.** Both stores key rows on `id` alone. The pipeline looks every pushed id up across banks first, and writes with `insert_vectors`, so even a race (two pushes of one id, or a push and a retain) can't overwrite: the loser is re-classified from the row that won. The same id twice in one batch: the second is `unchanged` or `rejected`.
- **Policy, as `/v1/retain`:** input size and tag limits, content validation, the PII barrier (redact rewrites the stored text; `reject` rejects that record only, not the batch), metadata sanitization, authoritative `_actor` from the authenticated caller. Underscore-prefixed metadata keys are system-owned: a push keeps `_created_at`, `_retain_id`, `_chunk_index` (the reader regroups chunks by them) and drops the rest (`_authority_tier`, `_mip.*`, …), which a client could otherwise use to change how recall ranks its memory. MIP routing is not applied: the bank is fixed by the URL and the ids.
- **Rate limits and quotas count a push as one call** (checked once, before anything is stored; 429 as usual), and quota usage is recorded per stored record, so a quota can be overshot by at most one batch. Counting each record against `retain_per_minute` would make a 100-record sync trip any realistic limit.
- **Request validation is 400**, like every other gateway endpoint (the gateway translates FastAPI's 422; see `models.py`): more than 100 records, an id outside `[A-Za-z0-9_-]{8,64}`, empty text, a malformed `content_hash`, non-scalar metadata values.
- **`content_hash`**, when sent, must equal `sha256:` + the hex SHA-256 of the UTF-8 text as sent (before any PII redaction); a mismatch rejects the record. It is not stored.
- **"Unchanged" compares the stored text** with the pushed text after PII redaction, so a redacted record re-pushed unchanged stays `unchanged`.

Client:

| PR | Content |
|---|---|
| **C1** | `astrocyte team join/status/sync/leave`: manual push and pull, preview, keychain token |

**C1 as built** (`astrocyte/harness/team.py`). Membership is per project bank, in `team.json` beside the config; the token goes to the OS keychain (`keyring`, optional) or that file (0600). Sync state per bank is in the state directory: the push ledger (`id → stored | unchanged | duplicate:<id> | rejected:<reason> | forgotten`), the ids pulled from teammates, and the feed cursor. Decisions made while building it:

- **Push sends `fact`-layer rows only**, never `observation`/`model` rows (derived locally, re-derivable on the server), and never a pulled id. A rejected or duplicate id is recorded and not re-sent; text is immutable, so it would get the same answer.
- **Pull writes through the local `push_records`** with no caller context, so the teammate's `_actor` (stamped by the gateway) is kept, and the local policy layer and dedup apply. A teammate's memory that near-duplicates a local one is skipped (counted, not stored). Server-made observations and mental models are not mirrored (§8, G3 left this to the client).
- **A tombstone erases the id here** (forget, then purge from disk), including a memory of yours that a teammate forgot for the team; your ledger then marks it `forgotten` so it is never pushed back.
- **Metadata changes after the first pull are not applied yet**: a later upsert of a pulled id is answered `unchanged` locally. Status, trust and staleness fields don't exist yet; when they do, they need a local update path (C2).
- **Each acknowledged push batch and each applied pull page is saved before the next request**, so a failure mid-sync resumes without re-sending or re-applying.
| **C2** | Background sync in the agent daemon; tombstones purge mirrors; forget scopes; attribution in boot and recall |

**C2 as built.** The agent daemon syncs every joined project on its first housekeeping pass and then every `ASTROCYTE_TEAM_SYNC_SECONDS` (300). Decisions made while building it:

- **One sync per bank at a time**, across processes: a lock file beside the bank's sync state. The daemon skips a bank another sync holds; the CLI says so. A failed sync keeps its error in the state (`team status` shows it); the next interval retries.
- **The daemon syncs through a second `Astrocyte` sharing its pipeline**, with noisy-bank detection off: a first pull of a team's memories is exactly the burst that check refuses, and the shared pipeline means no second embedding model.
- **"Teammate's memory" means an id this machine pulled**, from the sync state, not "has `_actor`": local memories saved through the MCP server carry `_actor` too (`agent:mcp`, or the configured principal). Labels are `(alice)` for `user:alice` and the full principal otherwise (`(service:ci)`), in session-start, prompt recall, file recall and `astrocyte memory list` (`saved_by` in `--json`).
- **"Where you left off" skips teammates' captured turns** (only shared when a project opts in), as §6 says: it is your own previous session.
- **Forget scopes.** `forget <id>` on a memory that is on the gateway (pulled, or pushed and kept) refuses without `--team` or `--local`. `--team` forgets it on the gateway first (`/v1/forget`, needs `forget`; a legal hold or 403 stops it before anything local changes), then here; the ledger marks it `forgotten`. `--local` erases it here and adds it to the bank's suppression list, which pull honours even when the feed is replayed from the start. `forget --all` on a shared project is local only and suppresses what it erased; a memory never shared is forgotten as before, with no prompt.
| **C3** | `share`/`unshare`; opt-in captured sharing; doctor's team section; docs |

**Testing.** An end-to-end CI job runs the gateway container with two local users (two HOMEs) and checks that:
- Alice's agent saves a decision, it syncs, and Bob's `session-start` and prompt recall show it, attributed to Alice;
- Alice runs `forget --team` and Bob's mirror purges the row from disk;
- a token without `forget` is refused;
- a push made offline is queued and drains later;
- a forged `_actor` is ignored.

## 9. Decisions (2026-10-04)

1. **Captured turns stay local by default.** A project opts in with `team.share_captured: true`; single turns can be shared with `astrocyte memory share <id>`.
2. **Auth: per-user gateway tokens** (G1) for teams without an identity provider; OIDC remains supported for those with one.
3. **Granularity: the whole project bank**, with per-memory `private` / `unshare` as the exception.
4. **Leaving a team purges teammates' memories** from the local mirror by default; `--keep-mirror` keeps them.

Added 2026-10-05, after the anchored-documents direction (`anchored-documents.md`):

5. **A shared claim cites a shareable source when one exists, and a redacted excerpt otherwise.** Every claim cites evidence, but captured turns stay local (1), so a citation to a private turn would dangle on teammates' machines. A team claim cites a document, commit or PR every teammate can open; when there is none, it carries the PII-redacted verbatim excerpt of the turn it came from, and that claim's trust is capped below one with a shareable source.
6. **C1 syncs memories, on G2/G3 as built.** Decisions, conventions and imported docs sync as memories now; claims and pages join the same push and changes feed later as rows with their own fields, rather than C1 waiting for the document layer.
