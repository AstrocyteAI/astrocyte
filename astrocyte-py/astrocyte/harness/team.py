"""``astrocyte team`` — share a project's memory through the team's gateway.

The client side of team memory (``docs/_design/team-memory.md``, C1): manual
push and pull, with a preview before anything leaves the machine. Hooks and
the MCP server keep reading and writing only the local store; this module
copies between it and the gateway's bank of the same id.

- **Push** sends the project's shareable memories (``/sync/push``), each as
  one row under its own id: what an agent saved and what was imported, never
  captured conversation turns (unless the project opts in) or anything tagged
  ``private``. Teammates' memories pulled here are never pushed back.
- **Pull** pages the bank's changes feed (``/changes``) from a saved cursor:
  a teammate's memory is stored locally under the same id, through the same
  policy layer as a push, keeping who saved it (``_actor``); a tombstone
  erases that id here, from disk, including a memory of yours that a
  teammate forgot for the team.

Membership (gateway URL per project bank) lives in ``team.json`` beside the
config; the token goes to the OS keychain when ``keyring`` is installed, else
into that file (0600). Sync state (the push ledger, the ids pulled, the feed
cursor) lives in the state directory, never in the memory store, so
``astrocyte memory export`` stays clean.
"""

from __future__ import annotations

import json
import os
import sys
from argparse import Namespace
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .paths import config_path, state_dir

KEYRING_SERVICE = "astrocyte-team"
PUSH_BATCH = 100  # the gateway's limit per request
PULL_PAGE = 500
PREVIEW_SAMPLE = 3
#: Metadata a push carries. Other underscore keys are the store's own, and the
#: gateway would drop them anyway.
_PUSHED_SYSTEM_KEYS = ("_created_at", "_retain_id", "_chunk_index")


class TeamError(Exception):
    """A problem the user can act on; printed without a traceback."""


# ── membership and token ─────────────────────────────────────────────────


def team_file(cfg_path: Path) -> Path:
    return cfg_path.parent / "team.json"


def load_memberships(cfg_path: Path) -> dict[str, dict[str, Any]]:
    """Bank id → ``{"url", "share_captured", "joined_at", "token"?}``."""
    try:
        data = json.loads(team_file(cfg_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    return {k: v for k, v in projects.items() if isinstance(v, dict)} if isinstance(projects, dict) else {}


def save_memberships(cfg_path: Path, projects: dict[str, dict[str, Any]]) -> None:
    path = team_file(cfg_path)
    if not projects:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # Created private: it may hold a token.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"projects": projects}, f, indent=2)
    os.replace(tmp, path)


def _keyring() -> Any | None:
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def _keychain_account(bank: str, url: str) -> str:
    # Per project and gateway: one person may hold differently scoped tokens
    # for two projects on one gateway, and leaving one must not drop the other.
    return f"{bank}@{url}"


def store_token(bank: str, url: str, token: str, entry: dict[str, Any]) -> str:
    """Keep ``token`` for this project's gateway: the OS keychain if
    available, else the membership entry (written 0600). Returns where."""
    kr = _keyring()
    if kr is not None:
        account = _keychain_account(bank, url)
        try:
            kr.set_password(KEYRING_SERVICE, account, token)
            # Read it back: some backends accept a password and keep nothing
            # (the null backend, a locked or broken store).
            if kr.get_password(KEYRING_SERVICE, account) == token:
                entry.pop("token", None)
                return "the OS keychain"
        except Exception:  # noqa: BLE001 — no usable backend (headless Linux, CI)
            pass
    entry["token"] = token
    return "team.json (readable only by you)"


def token_for(bank: str, entry: dict[str, Any]) -> str | None:
    if isinstance(entry.get("token"), str):
        return entry["token"]
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(KEYRING_SERVICE, _keychain_account(bank, entry["url"]))
    except Exception:  # noqa: BLE001
        return None


def forget_token(bank: str, url: str) -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(KEYRING_SERVICE, _keychain_account(bank, url))
    except Exception:  # noqa: BLE001 — not there, or no backend
        pass


# ── sync state ───────────────────────────────────────────────────────────


@dataclass
class SyncState:
    """Per bank: what each pushed id became, which ids came from teammates,
    and where the changes feed was left."""

    cursor: str | None = None
    #: id → "stored" | "unchanged" | "duplicate:<id>" | "rejected:<reason>" | "forgotten"
    pushed: dict[str, str] = field(default_factory=dict)
    pulled: list[str] = field(default_factory=list)
    #: ids erased here with ``forget --local``: a later pull must not bring them back
    suppressed: list[str] = field(default_factory=list)
    #: ids shared with ``memory share`` that the rules would keep here (a captured turn, ``private``)
    promoted: list[str] = field(default_factory=list)
    #: ids kept here with ``memory unshare``: never pushed, and their tombstone doesn't erase them here
    withheld: list[str] = field(default_factory=list)
    last_sync: str | None = None
    last_error: str | None = None

    @staticmethod
    def path(bank: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in bank)
        return state_dir() / "team" / f"{safe}.json"

    @classmethod
    def load(cls, bank: str) -> SyncState:
        try:
            data = json.loads(cls.path(bank).read_text(encoding="utf-8"))
            return cls(cursor=data.get("cursor"), pushed=dict(data.get("pushed") or {}),
                       pulled=list(data.get("pulled") or []), suppressed=list(data.get("suppressed") or []),
                       promoted=list(data.get("promoted") or []), withheld=list(data.get("withheld") or []),
                       last_sync=data.get("last_sync"), last_error=data.get("last_error"))
        except (OSError, ValueError, AttributeError, TypeError):
            return cls()

    def save(self, bank: str) -> None:
        path = self.path(bank)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"cursor": self.cursor, "pushed": self.pushed, "pulled": self.pulled,
                                   "suppressed": self.suppressed, "promoted": self.promoted,
                                   "withheld": self.withheld, "last_sync": self.last_sync,
                                   "last_error": self.last_error}), encoding="utf-8")
        os.replace(tmp, path)

    def shared(self, memory_id: str) -> bool:
        """Is this id on the gateway: pulled from a teammate, or pushed and kept?"""
        return memory_id in self.pulled or self.pushed.get(memory_id) in ("stored", "unchanged")


def pulled_ids(bank: str) -> set[str]:
    """Ids of teammates' memories in ``bank`` on this machine (empty when the
    project isn't shared)."""
    return set(SyncState.load(bank).pulled)


class SyncBusy(TeamError):
    """Another process (the agent daemon, or a second `astrocyte team`) is syncing this bank."""


class _BankLock:
    """One sync per bank at a time across processes: the daemon's background
    sync and a manual `astrocyte team sync` would otherwise interleave pages
    of the feed and both write the ledger."""

    def __init__(self, bank: str) -> None:
        self.path = SyncState.path(bank).with_suffix(".lock")
        self.fh: Any = None

    def __enter__(self) -> _BankLock:
        from .agentd import _lock_exclusively

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fh = open(self.path, "w")  # noqa: SIM115 — held until __exit__
        if not _lock_exclusively(self.fh):
            self.fh.close()
            raise SyncBusy("another sync of this project is running (the agent daemon syncs in the background)")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.fh.close()


# ── what is shared ───────────────────────────────────────────────────────


def shareable(item: Any, state: SyncState, *, share_captured: bool) -> bool:
    """Would this local memory go to the team? Saved and imported memories
    do; captured turns only where the project opted in; ``private`` never;
    nor anything pulled from a teammate. ``memory share`` and ``unshare``
    override the rules for one memory."""
    from astrocyte._sync import PUSH_ID_RE

    if item.id in state.withheld or item.id in state.pulled or (item.memory_layer or "fact") != "fact":
        return False
    tags = set(item.tags or ())
    if item.id not in state.promoted and ("private" in tags or ("captured" in tags and not share_captured)):
        return False
    return bool(PUSH_ID_RE.fullmatch(item.id))


def _record(item: Any) -> dict[str, Any]:
    from astrocyte._sync import content_hash

    metadata = {
        k: v for k, v in (item.metadata or {}).items()
        if (not k.startswith("_") or k in _PUSHED_SYSTEM_KEYS) and (v is None or isinstance(v, (str, int, float, bool)))
    }
    return {
        "id": item.id,
        "text": item.text,
        "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None,
        "tags": list(item.tags) if item.tags else None,
        "fact_type": item.fact_type,
        "metadata": metadata or None,
        "content_hash": content_hash(item.text),
    }


async def _all_items(store: Any, bank: str) -> list[Any]:
    items, offset = [], 0
    while True:
        page = await store.list_vectors(bank, offset=offset, limit=500)
        items += page
        if len(page) < 500:
            return items
        offset += 500


async def pending_push(store: Any, bank: str, state: SyncState, *, share_captured: bool) -> list[Any]:
    """Local memories that would go up on the next push, oldest first."""
    items = [i for i in await _all_items(store, bank)
             if i.id not in state.pushed and shareable(i, state, share_captured=share_captured)]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(items, key=lambda i: (i.retained_at or i.occurred_at or epoch, i.id))


# ── the gateway ──────────────────────────────────────────────────────────


def _client(url: str, token: str) -> Any:
    """An HTTP client for the gateway (tests substitute a transport)."""
    import httpx

    return httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=30.0)


def _bank_path(bank: str, rest: str) -> str:
    return f"/v1/banks/{quote(bank, safe='')}/{rest}"


async def _call(client: Any, method: str, path: str, **kw: Any) -> Any:
    import httpx

    try:
        response = await client.request(method, path, **kw)
    except httpx.HTTPError as e:
        raise TeamError(f"can't reach the gateway at {client.base_url}: {e}") from None
    if response.status_code == 401:
        raise TeamError("the gateway refused the token (401): check it, or ask for a new one")
    if response.status_code == 403:
        raise TeamError(f"the token has no access to this project's bank (403): {_detail(response)}")
    if response.status_code == 501:
        raise TeamError("this gateway's storage doesn't support team sync (501): it needs the Postgres or "
                        "SQLite store")
    if response.status_code >= 400:
        raise TeamError(f"the gateway answered {response.status_code}: {_detail(response)}")
    return response.json()


def _detail(response: Any) -> str:
    try:
        return str(response.json().get("detail") or response.text)[:300]
    except ValueError:
        return response.text[:300]


async def probe(client: Any, bank: str) -> None:
    """Reachable, the token accepted, and ``read`` on the bank."""
    await _call(client, "GET", _bank_path(bank, "changes"), params={"limit": 1})


async def check(bank: str, entry: dict[str, Any]) -> None:
    """:func:`probe` a joined project's gateway with its saved token."""
    async with _connect(bank, entry) as client:
        await probe(client, bank)


@dataclass
class SyncReport:
    pushed: dict[str, int] = field(default_factory=dict)  # status → count
    pulled: int = 0
    erased: int = 0
    skipped: int = 0  # duplicates of a local memory, or refused by the local policy

    def summary(self) -> str:
        sent = sum(self.pushed.values())
        parts = [f"pushed {sent}" + (f" ({_counts(self.pushed)})" if sent else "")]
        parts.append(f"pulled {self.pulled}")
        if self.erased:
            parts.append(f"erased {self.erased} forgotten by the team")
        if self.skipped:
            parts.append(f"skipped {self.skipped} already here")
        return ", ".join(parts)


def _counts(by_status: dict[str, int]) -> str:
    return ", ".join(f"{n} {status}" for status, n in sorted(by_status.items()) if n)


async def push(client: Any, store: Any, bank: str, state: SyncState, *, share_captured: bool,
               report: SyncReport) -> None:
    items = await pending_push(store, bank, state, share_captured=share_captured)
    for start in range(0, len(items), PUSH_BATCH):
        batch = items[start:start + PUSH_BATCH]
        reply = await _call(client, "POST", _bank_path(bank, "sync/push"),
                            json={"records": [_record(i) for i in batch]})
        for result in reply.get("results") or []:
            status = result.get("status")
            if status == "duplicate":
                state.pushed[result["id"]] = f"duplicate:{result.get('duplicate_of')}"
            elif status == "rejected":
                state.pushed[result["id"]] = f"rejected:{result.get('reason')}"
            else:
                state.pushed[result["id"]] = str(status)
            report.pushed[status] = report.pushed.get(status, 0) + 1
        state.save(bank)  # each acknowledged batch, so a failure later never re-sends it


async def pull(client: Any, pipeline: Any, brain: Any, bank: str, state: SyncState, report: SyncReport,
               *, apply: bool = True) -> int:
    """Apply the changes feed from the saved cursor; returns how many changes
    it read. ``apply=False`` only counts them (and moves no cursor)."""
    from astrocyte.types import SyncPushRecord

    cursor, seen = state.cursor, 0
    pulled = set(state.pulled)
    while True:
        params: dict[str, Any] = {"limit": PULL_PAGE}
        if cursor:
            params["cursor"] = cursor
        page = await _call(client, "GET", _bank_path(bank, "changes"), params=params)
        changes = page.get("changes") or []
        seen += len(changes)
        if apply and changes:
            erase: list[str] = []
            incoming: list[SyncPushRecord] = []
            for c in changes:
                cid = c.get("id")
                if not isinstance(cid, str):
                    continue
                if c.get("deleted"):
                    if cid in state.withheld:
                        continue  # our own memory, taken off the team with `unshare`: it stays here
                    erase.append(cid)
                    continue
                own = cid in state.pushed and not state.pushed[cid].startswith(("duplicate:", "rejected:"))
                if own or cid in state.suppressed or (c.get("memory_layer") or "fact") != "fact" or not c.get("text"):
                    continue  # ours already; server-made observations and models aren't mirrored
                incoming.append(SyncPushRecord(
                    id=cid, text=c["text"], occurred_at=_parse_time(c.get("occurred_at")),
                    tags=c.get("tags") or None, fact_type=c.get("fact_type"),
                    metadata={k: v for k, v in (c.get("metadata") or {}).items()
                              if v is None or isinstance(v, (str, int, float, bool))} or None,
                ))
            if incoming:
                for result in await brain.push_records(bank, incoming):
                    if result.status in ("stored", "unchanged"):
                        if result.id not in pulled:
                            pulled.add(result.id)
                            state.pulled.append(result.id)
                        report.pulled += result.status == "stored"
                    else:
                        report.skipped += 1
            if erase:
                report.erased += await _erase_local(pipeline, brain, bank, erase)
                for cid in erase:
                    if cid in pulled:
                        pulled.discard(cid)
                        state.pulled.remove(cid)
                    if cid in state.pushed:
                        state.pushed[cid] = "forgotten"
        cursor = page.get("next_cursor") or cursor
        if apply:
            state.cursor = cursor
            state.save(bank)
        if not page.get("has_more"):
            return seen


async def _erase_local(pipeline: Any, brain: Any, bank: str, ids: list[str]) -> int:
    """Forget and purge ``ids`` here, as ``astrocyte memory forget`` does."""
    present = set(await _existing(pipeline.vector_store, bank, ids))
    if not present:
        return 0
    result = await brain.forget(bank, memory_ids=sorted(present))
    purge = getattr(pipeline.vector_store, "purge", None)
    if purge is not None:
        await purge(bank, sorted(present))
    return result.deleted_count


async def _existing(store: Any, bank: str, ids: list[str]) -> list[str]:
    """Which of ``ids`` are live memories in ``bank`` here."""
    lookup = getattr(store, "lookup_ids", None)
    if lookup is not None:
        return [c.id for c in await lookup(ids) if not c.deleted and c.bank_id == bank]
    wanted = set(ids)
    return [i.id for i in await _all_items(store, bank) if i.id in wanted]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def forget_shared(entry: dict[str, Any], bank: str, ids: list[str], pipeline: Any, brain: Any, *,
                        team_wide: bool) -> int:
    """Erase ``ids`` here; with ``team_wide``, first on the gateway too, so
    every teammate's next sync erases them (needs ``forget`` on the bank).
    Without it, the ids are suppressed so a later pull doesn't bring them
    back. Returns how many memories were erased here."""
    with _BankLock(bank):
        state = SyncState.load(bank)
        shared = [i for i in ids if state.shared(i)]
        if team_wide and shared:
            async with _connect(bank, entry) as client:
                await _call(client, "POST", "/v1/forget", json={"bank_id": bank, "memory_ids": shared})
        erased = await _erase_local(pipeline, brain, bank, ids)
        for mid in ids:
            if mid in state.pulled:
                state.pulled.remove(mid)
            if team_wide and mid in state.pushed:
                state.pushed[mid] = "forgotten"
            if not team_wide and mid in shared and mid not in state.suppressed:
                state.suppressed.append(mid)
        state.save(bank)
        return erased


def _same_retain(items: list[Any], ids: list[str]) -> list[str]:
    """``ids`` plus every chunk retained with them: a long turn is several
    rows sharing a ``_retain_id``, and sharing half of one makes no sense."""
    chosen = set(ids)
    keys = {(i.metadata or {}).get("_retain_id") for i in items if i.id in chosen} - {None}
    return [i.id for i in items if i.id in chosen or (i.metadata or {}).get("_retain_id") in keys]


async def share(bank: str, ids: list[str], store: Any) -> list[str]:
    """Share these memories (and their chunks) at the next push, whatever the
    rules say. Returns the ids that will go up; a teammate's are skipped."""
    with _BankLock(bank):
        state = SyncState.load(bank)
        ids = [i for i in _same_retain(await _all_items(store, bank), ids) if i not in state.pulled]
        gone = [i for i in ids if state.pushed.get(i) in ("unshared", "forgotten")]
        if gone:
            # The gateway keeps a forgotten id forgotten: a re-push is rejected.
            raise TeamError(f"{gone[0][:8]} was taken off the team's gateway, and a forgotten memory can't be "
                            "shared again under its id")
        for mid in ids:
            if mid in state.withheld:
                state.withheld.remove(mid)
            if mid not in state.promoted:
                state.promoted.append(mid)
        state.save(bank)
        return ids


async def unshare(entry: dict[str, Any] | None, bank: str, ids: list[str], store: Any) -> tuple[list[str], int]:
    """Keep these memories (and their chunks) on this machine. One already
    on the gateway is forgotten there (needs ``forget``), so every teammate's
    copy goes at their next sync; yours stays. Returns (ids kept here, how
    many were taken off the gateway). A teammate's memory can't be unshared."""
    with _BankLock(bank):
        state = SyncState.load(bank)
        ids = _same_retain(await _all_items(store, bank), ids)
        theirs = [i for i in ids if i in state.pulled]
        if theirs:
            raise TeamError(f"{theirs[0][:8]} is a teammate's memory: hide it here with "
                            "`astrocyte memory forget <id> --local`")
        on_gateway = [i for i in ids if state.pushed.get(i) in ("stored", "unchanged")]
        if on_gateway:
            if entry is None:
                raise TeamError("these memories are on the team's gateway, but this project isn't joined any more")
            async with _connect(bank, entry) as client:
                await _call(client, "POST", "/v1/forget", json={"bank_id": bank, "memory_ids": on_gateway})
        for mid in ids:
            if mid in state.promoted:
                state.promoted.remove(mid)
            if mid not in state.withheld:
                state.withheld.append(mid)
            if mid in on_gateway:
                state.pushed[mid] = "unshared"
        state.save(bank)
        return ids, len(on_gateway)


# ── commands ─────────────────────────────────────────────────────────────


def _bank(args: Namespace) -> str:
    from .project import project_bank

    return args.bank or project_bank(str(Path(args.project or os.getcwd()).expanduser().resolve()))


def _membership(cfg: Path, bank: str) -> dict[str, Any]:
    entry = load_memberships(cfg).get(bank)
    if entry is None:
        raise TeamError(f"{bank} isn't shared with a team. Join first: astrocyte team join <gateway-url> --token …")
    return entry


def _connect(bank: str, entry: dict[str, Any]) -> Any:
    token = token_for(bank, entry)
    if not token:
        raise TeamError(f"no token for {entry['url']}: run astrocyte team join again")
    return _client(entry["url"], token)


def _preview(items: list[Any]) -> None:
    from .agentd import render_memory

    for item in items[:PREVIEW_SAMPLE]:
        print("    " + render_memory(item.text, None).removeprefix("- ")[:110])
    if len(items) > PREVIEW_SAMPLE:
        print(f"    … and {len(items) - PREVIEW_SAMPLE} more")


async def sync_bank(entry: dict[str, Any], bank: str, pipeline: Any, brain: Any) -> SyncReport:
    """Push, then pull, one joined bank; the state records the outcome either
    way. Raises :class:`SyncBusy` if another process is syncing it."""
    with _BankLock(bank):
        state = SyncState.load(bank)
        report = SyncReport()
        try:
            async with _connect(bank, entry) as client:
                await push(client, pipeline.vector_store, bank, state,
                           share_captured=bool(entry.get("share_captured")), report=report)
                await pull(client, pipeline, brain, bank, state, report)
        except TeamError as e:
            state = SyncState.load(bank)  # keep what push and pull saved as they went
            state.last_error = str(e)
            state.save(bank)
            raise
        state.last_sync = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state.last_error = None
        state.save(bank)
        return report


async def _sync(entry: dict[str, Any], bank: str, pipeline: Any, brain: Any, *, dry_run: bool) -> int:
    if dry_run:
        state = SyncState.load(bank)
        async with _connect(bank, entry) as client:
            items = await pending_push(pipeline.vector_store, bank, state,
                                       share_captured=bool(entry.get("share_captured")))
            print(f"Would push {len(items)} memories to {entry['url']}:")
            _preview(items)
            waiting = await pull(client, pipeline, brain, bank, state, SyncReport(), apply=False)
            print(f"Would pull {waiting} changes from the team.")
        return 0
    report = await sync_bank(entry, bank, pipeline, brain)
    print(f"Synced {bank}: {report.summary()}.")
    return 0


async def _join(args: Namespace, cfg: Path, pipeline: Any, brain: Any) -> int:
    bank = _bank(args)
    url = args.url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise TeamError(f"{args.url}: give the gateway's URL, e.g. https://memory.example.com")
    token = args.token or os.environ.get("ASTROCYTE_TEAM_TOKEN")
    if not token:
        raise TeamError("give the token the gateway's admin created for you: --token … (or ASTROCYTE_TEAM_TOKEN)")
    async with _client(url, token) as client:
        await probe(client, bank)
    entry = {"url": url, "share_captured": bool(args.share_captured),
             "joined_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    state = SyncState.load(bank)
    items = await pending_push(pipeline.vector_store, bank, state, share_captured=entry["share_captured"])
    already = load_memberships(cfg).get(bank)
    print(f"Updating {bank}'s team settings ({url})." if already else f"Joining {bank} at {url}.")
    if items:
        print(f"This shares {len(items)} memories from this machine with everyone who can read the bank"
              + (" (captured conversation included)" if entry["share_captured"] else
                 " (saved and imported ones; captured conversation stays here)") + ":")
        _preview(items)
        if not args.yes:
            if not sys.stdin.isatty():
                print("Re-run with --yes to share them.", file=sys.stderr)
                return 1
            if input("Share them? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Not joined; nothing left this machine.")
                return 1
    else:
        print("Nothing on this machine to share yet; teammates' memories will be pulled.")
    projects = load_memberships(cfg)
    where = store_token(bank, url, token, entry)
    projects[bank] = entry
    save_memberships(cfg, projects)
    print(f"Token kept in {where}.")
    return await _sync(entry, bank, pipeline, brain, dry_run=False)


async def _status(args: Namespace, cfg: Path, pipeline: Any, _brain: Any) -> int:
    bank = _bank(args)
    entry = _membership(cfg, bank)
    state = SyncState.load(bank)
    waiting = await pending_push(pipeline.vector_store, bank, state, share_captured=bool(entry.get("share_captured")))
    outcomes: dict[str, int] = {}
    for status in state.pushed.values():
        key = status.split(":", 1)[0]
        outcomes[key] = outcomes.get(key, 0) + 1
    print(f"{bank} ↔ {entry['url']}")
    print(f"  last sync      {state.last_sync or 'never'}")
    if state.last_error:
        print(f"  last error     {state.last_error}")
    print(f"  pushed         {sum(outcomes.values())}" + (f" ({_counts(outcomes)})" if outcomes else ""))
    print(f"  waiting to go  {len(waiting)}")
    print(f"  from the team  {len(state.pulled)}")
    print("  captured turns " + ("shared" if entry.get("share_captured") else "stay on this machine"))
    try:
        await check(bank, entry)
        print("  gateway        reachable, token accepted")
    except TeamError as e:
        print(f"  gateway        {e}")
        return 1
    return 0


async def _sync_cmd(args: Namespace, cfg: Path, pipeline: Any, brain: Any) -> int:
    bank = _bank(args)
    return await _sync(_membership(cfg, bank), bank, pipeline, brain, dry_run=args.dry_run)


async def _leave(args: Namespace, cfg: Path, pipeline: Any, brain: Any) -> int:
    bank = _bank(args)
    entry = _membership(cfg, bank)
    state = SyncState.load(bank)
    erased = 0
    if state.pulled and not args.keep_mirror:
        erased = await _erase_local(pipeline, brain, bank, list(state.pulled))
    projects = load_memberships(cfg)
    projects.pop(bank, None)
    save_memberships(cfg, projects)
    forget_token(bank, entry["url"])
    SyncState.path(bank).unlink(missing_ok=True)
    kept = (f"; kept {len(state.pulled)} teammates' memories (--keep-mirror)" if args.keep_mirror and state.pulled
            else f"; erased {erased} teammates' memories from this machine" if erased else "")
    print(f"Left the team for {bank}{kept}. Your own memories stay here and on the gateway.")
    return 0


_COMMANDS = {"join": _join, "status": _status, "sync": _sync_cmd, "leave": _leave}


def run(args: Namespace) -> int:
    from .memories import open_local

    cfg = Path(args.config).expanduser() if args.config else config_path()
    if not cfg.is_file():
        print(f"No Astrocyte config at {cfg}. Run: astrocyte setup", file=sys.stderr)
        return 2
    if not args.team_command:
        print("astrocyte team join|status|sync|leave — see astrocyte team --help", file=sys.stderr)
        return 2
    pipeline, brain = open_local(cfg, noisy_bank_detection=False)

    async def go() -> int:
        try:
            return await _COMMANDS[args.team_command](args, cfg, pipeline, brain)
        except TeamError as e:
            print(f"astrocyte team: {e}", file=sys.stderr)
            return 1
        finally:
            close = getattr(pipeline.vector_store, "close", None)
            if close is not None:
                await close()

    import asyncio  # here, not at import: `astrocyte hook` builds this parser too

    return asyncio.run(go())


def register(sub) -> None:
    team = sub.add_parser(
        "team",
        help="Share this project's memory with your team through an Astrocyte gateway",
        description="Saved and imported memories are shared; captured conversation stays on this machine "
        "unless the project opts in. Teammates' memories are pulled into the local store, so recall stays "
        "local and works offline.",
    )
    team.set_defaults(func=run, team_command=None)

    def scope(p) -> None:
        p.add_argument("--project", help="project directory (default: the current directory)")
        p.add_argument("--bank", help="a bank id instead of a project")
        p.add_argument("--config", help="config path (default: ~/.config/astrocyte/astrocyte.yaml)")

    scope(team)
    cmds = team.add_subparsers(dest="team_command")
    join = cmds.add_parser("join", help="share this project through a gateway: previews, then confirms")
    join.add_argument("url", help="the gateway, e.g. https://memory.example.com")
    join.add_argument("--token", help="your token from the gateway's admin (or ASTROCYTE_TEAM_TOKEN)")
    join.add_argument("--share-captured", action="store_true",
                      help="also share captured conversation turns (off by default)")
    join.add_argument("--yes", action="store_true", help="don't ask before the first push")
    scope(join)
    scope(cmds.add_parser("status", help="gateway, last sync, what is waiting to go up"))
    sync = cmds.add_parser("sync", help="push and pull now")
    sync.add_argument("--dry-run", action="store_true", help="show what would go up and how much would come down")
    scope(sync)
    leave = cmds.add_parser("leave", help="stop sharing this project; erases teammates' memories here")
    leave.add_argument("--keep-mirror", action="store_true", help="keep teammates' memories on this machine")
    scope(leave)
