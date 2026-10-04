# Team memory

Status: **accepted** (October 2026; decisions in §9). G1 (gateway auth) and G3 (changes feed) implemented; G2 and the rest not yet.

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

**Conflicts.** Memories are immutable facts with timestamps; nobody edits a memory, so there are no write conflicts to resolve. Two teammates can still disagree ("we use SQS" vs. a later "moved to Kafka"). That is the same problem as one person changing their mind, and recall already handles it with recency (`occurred_at`) and observation supersession. Out of scope here.

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
| **G2** | `POST /v1/banks/{bank}/sync/push`: batch upsert with client ids through the policy layer; no re-chunking; per-record `stored \| duplicate_of \| rejected` |
| **G3** (implemented) | `GET /v1/banks/{bank}/changes?cursor=&limit=`: upserts and tombstones in order. Stores gain a `changed_at` column (`max(retained_at, forgotten_at)`) and an index on `(bank_id, changed_at, id)` |
| **G4** | Persisted legal holds; forget by `_actor` (DSAR) |

**G3 as built.** `Astrocyte.list_changes(bank_id, cursor=, limit=, settle_seconds=)` over an optional `VectorStore.list_changes(bank_id, *, after, limit)`; the gateway serves it at `GET /v1/banks/{bank_id}/changes` (needs `read`; 501 when the store has no feed). Decisions the plan left open:

- **Postgres `changed_at` is an indexed expression, not a column.** Migration 039 indexes `(bank_id, GREATEST(retained_at, forgotten_at), id COLLATE "C")` and the store queries that exact expression. A stored column would need a backfill, and on `astrocyte_vectors` either way of doing one rewrites every row: a STORED generated column rewrites the table under an exclusive lock and rebuilds the DiskANN index with it; an UPDATE backfill inserts a new version of every row into that index. The expression is also correct for every write path by construction. SQLite keeps a real `changed_at` column (added and backfilled on open; local stores are small), and the in-memory store remembers tombstones beside its hard deletes.
- **Ids order byte-wise** (`COLLATE "C"` on Postgres, `BINARY` in SQLite), so the order and the cursor don't depend on the database locale, and SQLite and Postgres agree (the parity suite checks mixed-case and `_`/`-` ids).
- **Cursor.** URL-safe base64 of `{"changed_at", "id"}`, opaque to clients; one the server can't parse is a 400 (`InvalidCursor`). A page returns `next_cursor` (the position after its last change, or the request's own cursor when empty, so a caught-up client keeps its place) and `has_more`.
- **Tombstones carry nothing but `id`, `deleted` and `changed_at`**, so a forgotten memory's text never leaves the gateway again. A live entry includes `memory_layer` as well as the planned fields, so a client can tell server-side observations and mental models from teammates' memories (whether mirrors should pull those is for C1/C2).
- **Settle window.** `changed_at` is stamped just before a write commits, so with concurrent writers a slow commit could land behind a cursor that already moved past it, and that row would never be pulled. The gateway holds back changes younger than `ASTROCYTE_CHANGES_SETTLE_SECONDS` (default 5 s; the library default is 0). This assumes commits take less than the window and that the gateway hosts' clocks agree; a feed that needs a hard guarantee would order by a commit-time sequence instead.
- **Retention of tombstones.** Soft-deleted rows (and so tombstones) are kept indefinitely today; nothing purges them on the gateway. If a purge is added, a mirror whose cursor is older than the purge horizon must re-sync from scratch. The local SQLite store's `astrocyte memory forget` purges rows outright and so leaves no tombstone, which is fine: it doesn't serve a feed.

Client:

| PR | Content |
|---|---|
| **C1** | `astrocyte team join/status/sync/leave`: manual push and pull, preview, keychain token |
| **C2** | Background sync in the agent daemon; tombstones purge mirrors; forget scopes; attribution in boot and recall |
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
