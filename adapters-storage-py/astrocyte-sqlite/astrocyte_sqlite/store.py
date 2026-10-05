"""SQLite-backed VectorStore + DocumentStore.

Why this exists
---------------
"Install it and it works in your coding agent" needs storage with no server
to run. This adapter keeps every memory in one SQLite file so a developer can
``pip install`` and start retaining immediately.

Semantics are matched to ``PostgresStore``, not ``InMemoryVectorStore``
-----------------------------------------------------------------------
The two existing backends disagree in several places (the in-memory store lets
untagged items through a tag filter, never soft-deletes, returns unclamped
cosine, and applies ``time_range``/``session_id`` in ``search_similar`` where
Postgres does not). Postgres is what users deploy and what every benchmark
measures, so it is the contract here: identical config should recall
identically whichever of the two production backends holds the data.
``tests/test_parity_postgres.py`` enforces this differentially.

Design choices
--------------
* **No native extensions.** Embeddings are float32 BLOBs (pgvector's ``vector``
  is also float4) and similarity is exact cosine in numpy. ``sqlite-vec`` would
  need ``enable_load_extension``, which some Python builds omit. Exact search
  also out-recalls ANN at personal-memory scale.
* **FTS5 when present, LIKE fallback otherwise.** Keyword queries drop the same
  English stopwords ``plainto_tsquery('english', …)`` drops and AND the rest,
  so "what is the database" matches like it does on Postgres.
* **Multi-process safe.** An MCP server and short-lived hook processes write
  the same file concurrently: WAL journal, ``BEGIN IMMEDIATE`` for writes, a
  busy timeout, and a fresh connection per call (sqlite3 connections are
  thread-affine and cheap to open).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

import numpy as np
from astrocyte.types import (
    Document,
    DocumentFilters,
    DocumentHit,
    HealthStatus,
    VectorFilters,
    VectorHit,
    VectorItem,
)

if TYPE_CHECKING:
    # Imported where used: MemoryChange is newer than this package's
    # ``astrocyte`` floor, and only an astrocyte that has it calls list_changes.
    from astrocyte.types import MemoryChange

T = TypeVar("T")

DEFAULT_DB_PATH = "~/.local/share/astrocyte/astrocyte.db"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_US = timedelta(microseconds=1)

# PostgreSQL's ``english`` text-search stopword list (snowball english.stop).
# plainto_tsquery('english', q) discards these before ANDing the remaining
# terms; FTS5 would otherwise require every one of them to appear.
_ENGLISH_STOPWORDS = frozenset(
    """
    i me my myself we our ours ourselves you your yours yourself yourselves he
    him his himself she her hers herself it its itself they them their theirs
    themselves what which who whom this that these those am is are was were be
    been being have has had having do does did doing a an the and but if or
    because as until while of at by for with about against between into through
    during before after above below to from up down in out on off over under
    again further then once here there when where why how all any both each few
    more most other some such no nor not only own same so than too very s t can
    will just don should now
    """.split()
)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS astrocyte_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS astrocyte_vectors (
    pk           INTEGER PRIMARY KEY,
    id           TEXT    NOT NULL UNIQUE,
    bank_id      TEXT    NOT NULL,
    embedding    BLOB    NOT NULL,
    text         TEXT    NOT NULL,
    metadata     TEXT,
    tags         TEXT,
    fact_type    TEXT,
    occurred_at  INTEGER,
    memory_layer TEXT,
    retained_at  INTEGER NOT NULL,
    forgotten_at INTEGER,
    chunk_id     TEXT,
    changed_at   INTEGER
);
CREATE INDEX IF NOT EXISTS astrocyte_vectors_bank_live
    ON astrocyte_vectors (bank_id, forgotten_at);
CREATE INDEX IF NOT EXISTS astrocyte_vectors_bank_chunk
    ON astrocyte_vectors (bank_id, chunk_id);
-- What ``erase`` leaves of a forgotten memory: its id, bank and the time of
-- the forget, so the change feed keeps serving the tombstone and the id is
-- never stored again; text, embedding and metadata are gone.
CREATE TABLE IF NOT EXISTS astrocyte_tombstones (
    id         TEXT    PRIMARY KEY,
    bank_id    TEXT    NOT NULL,
    changed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS astrocyte_tombstones_bank_changed
    ON astrocyte_tombstones (bank_id, changed_at, id);
"""

# Created after the upgrade step below: on a database that predates
# ``changed_at``, the column has to exist before it can be indexed.
_CHANGES_INDEX = """
CREATE INDEX IF NOT EXISTS astrocyte_vectors_bank_changed
    ON astrocyte_vectors (bank_id, changed_at, id);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS astrocyte_vectors_fts USING fts5(
    text, content='astrocyte_vectors', content_rowid='pk',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS astrocyte_vectors_fts_ai
AFTER INSERT ON astrocyte_vectors BEGIN
    INSERT INTO astrocyte_vectors_fts(rowid, text) VALUES (new.pk, new.text);
END;
CREATE TRIGGER IF NOT EXISTS astrocyte_vectors_fts_ad
AFTER DELETE ON astrocyte_vectors BEGIN
    INSERT INTO astrocyte_vectors_fts(astrocyte_vectors_fts, rowid, text)
    VALUES ('delete', old.pk, old.text);
END;
CREATE TRIGGER IF NOT EXISTS astrocyte_vectors_fts_au
AFTER UPDATE OF text ON astrocyte_vectors BEGIN
    INSERT INTO astrocyte_vectors_fts(astrocyte_vectors_fts, rowid, text)
    VALUES ('delete', old.pk, old.text);
    INSERT INTO astrocyte_vectors_fts(rowid, text) VALUES (new.pk, new.text);
END;
"""

_HIT_COLUMNS = "id, text, metadata, tags, fact_type, occurred_at, memory_layer, retained_at, chunk_id"
_CHANGE_COLUMNS = (
    "id, bank_id, text, metadata, tags, fact_type, occurred_at, memory_layer, retained_at, forgotten_at, changed_at"
)

# ?10 reuses retained_at: a new row's changed_at. ?12 (upsert only) is the
# write time, the changed_at of an existing row being overwritten.
_INSERT_VECTOR = """
    INSERT INTO astrocyte_vectors (
        id, bank_id, embedding, text, metadata, tags, fact_type,
        occurred_at, memory_layer, retained_at, chunk_id, forgotten_at, changed_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?10)
"""
_ON_CONFLICT_UPSERT = """
    ON CONFLICT(id) DO UPDATE SET
        bank_id = excluded.bank_id,
        embedding = excluded.embedding,
        text = excluded.text,
        metadata = excluded.metadata,
        tags = excluded.tags,
        fact_type = excluded.fact_type,
        occurred_at = excluded.occurred_at,
        memory_layer = excluded.memory_layer,
        retained_at = excluded.retained_at,
        chunk_id = excluded.chunk_id,
        forgotten_at = NULL,
        changed_at = MAX(excluded.retained_at, ?12)
"""
_ITEM_COLUMNS = "id, bank_id, embedding, text, metadata, tags, fact_type, occurred_at, memory_layer, retained_at"


# ── value conversion ─────────────────────────────────────────────────────


def _to_us(dt: datetime | None) -> int | None:
    """Exact integer microseconds since the epoch (naive datetimes are UTC,
    as ``timestamptz`` treats them under a UTC session)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - _EPOCH) // _US


def _from_us(us: int | None) -> datetime | None:
    return None if us is None else _EPOCH + timedelta(microseconds=us)


def _now_us() -> int:
    return _to_us(datetime.now(UTC))  # type: ignore[return-value]


def _encode_vector(vec: list[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _decode_vector(blob: bytes) -> list[float]:
    return np.frombuffer(blob, dtype=np.float32).tolist()


def _encode_tags(tags: list[str] | None) -> str | None:
    return json.dumps(list(tags)) if tags is not None else None


def _decode_tags(raw: str | None) -> list[str] | None:
    # PostgresStore returns None for both NULL and the empty array.
    if not raw:
        return None
    tags = json.loads(raw)
    return list(tags) if tags else None


def _decode_metadata(raw: str | None) -> Any:
    return json.loads(raw) if raw is not None else None


def _keyword_terms(query: str) -> list[str]:
    """Terms plainto_tsquery('english', …) would keep, in query order."""
    seen: set[str] = set()
    terms: list[str] = []
    for tok in _TOKEN_RE.findall(query.lower()):
        if tok in _ENGLISH_STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        terms.append(tok)
    return terms


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _create_private(db: Path) -> None:
    """Create the database owner-only, before SQLite does with the umask
    (typically world-readable). Memories hold conversations; on a shared
    machine every account could read them. SQLite gives its -wal / -shm
    files the database file's permissions, so they follow. An existing file
    is left as it is — `astrocyte doctor --fix` tightens those.
    """
    if not db.parent.exists():
        db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not db.exists():
        try:
            os.close(os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            pass  # another process created it first


def _upgrade_schema(conn: sqlite3.Connection) -> None:
    """Bring a database created by an earlier version up to ``_SCHEMA``.

    ``CREATE TABLE IF NOT EXISTS`` leaves an existing table as it was, so
    columns added since are added here, each with a backfill, under a write
    lock so concurrent processes opening the same old file don't race.

    * ``changed_at`` (team-memory change feed): the row's last change to any
      synced field, set by every write; backfilled as ``max(retained_at,
      forgotten_at)``.
    """

    def has_changed_at() -> bool:
        return any(r["name"] == "changed_at" for r in conn.execute("PRAGMA table_info(astrocyte_vectors)"))

    if has_changed_at():
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not has_changed_at():  # another process may have upgraded first
            conn.execute("ALTER TABLE astrocyte_vectors ADD COLUMN changed_at INTEGER")
            conn.execute(
                "UPDATE astrocyte_vectors SET changed_at = MAX(retained_at, COALESCE(forgotten_at, retained_at))"
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ── store ────────────────────────────────────────────────────────────────


class SqliteStore:
    """VectorStore + DocumentStore over a single SQLite file.

    Args:
        path: Database file. Defaults to ``ASTROCYTE_SQLITE_PATH`` or
            ``~/.local/share/astrocyte/astrocyte.db``. Parent directories are
            created owner-only (0700 / 0600). ``:memory:`` is rejected: every call opens its own
            connection, so an in-memory database would vanish between calls.
        embedding_dimensions: Expected vector length. When omitted, the first
            write records it and every later write/query is checked against
            it, so switching embedding models fails loudly instead of silently
            mixing incomparable vectors.
        busy_timeout_ms: How long a writer waits for another process's lock.
    """

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        path: str | None = None,
        embedding_dimensions: int | None = None,
        busy_timeout_ms: int = 10_000,
        **_: Any,
    ) -> None:
        raw = path or os.environ.get("ASTROCYTE_SQLITE_PATH") or DEFAULT_DB_PATH
        if raw.strip() == ":memory:":
            raise ValueError(
                "SqliteStore does not support ':memory:' (each call opens its own "
                "connection). Use a file path, or the in_memory store for tests."
            )
        self._path = str(Path(raw).expanduser())
        self._configured_dim = int(embedding_dimensions) if embedding_dimensions else None
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._fts = False
        self._dim: int | None = self._configured_dim

    # ── plumbing ──────────────────────────────────────────────────────

    @property
    def path(self) -> str:
        return self._path

    @property
    def fts_enabled(self) -> bool:
        self._ensure_schema()
        return self._fts

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: autocommit, so transactions are explicit.
        conn = sqlite3.connect(self._path, timeout=self._busy_timeout_ms / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return conn

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            _create_private(Path(self._path))
            conn = self._connect()
            try:
                # WAL persists in the file: readers never block the writer, and
                # concurrent hook processes don't trip "database is locked".
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
                _upgrade_schema(conn)
                conn.executescript(_CHANGES_INDEX)
                try:
                    conn.executescript(_FTS_SCHEMA)
                    self._fts = True
                except sqlite3.OperationalError:
                    self._fts = False  # SQLite built without FTS5 → LIKE fallback
                row = conn.execute("SELECT value FROM astrocyte_meta WHERE key = 'embedding_dimensions'").fetchone()
                recorded = int(row["value"]) if row else None
                if recorded and self._configured_dim and recorded != self._configured_dim:
                    raise ValueError(
                        f"{self._path} holds {recorded}-dim embeddings but "
                        f"embedding_dimensions={self._configured_dim} is configured. "
                        "Use the embedding model that built this store, or a new file."
                    )
                self._dim = self._configured_dim or recorded
            finally:
                conn.close()
            self._schema_ready = True

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.to_thread(fn, *args)

    def _check_dim(self, n: int, what: str) -> None:
        if self._dim is not None and n != self._dim:
            raise ValueError(f"{what} length {n} != embedding_dimensions {self._dim}")

    @staticmethod
    def _live_filters(filters: VectorFilters | None, *, with_time_range: bool) -> tuple[list[str], list[Any]]:
        """WHERE clauses shared by search/list paths, mirroring PostgresStore.

        ``with_time_range`` reproduces a deliberate asymmetry in the reference:
        ``list_recent_vectors`` honours ``time_range`` but ``search_similar``
        does not. Neither applies ``session_id`` or ``metadata_filters``.
        """
        where: list[str] = []
        params: list[Any] = []
        if filters and filters.as_of:
            as_of = _to_us(filters.as_of)
            where.append("retained_at <= ?")
            where.append("(forgotten_at IS NULL OR forgotten_at > ?)")
            params.extend([as_of, as_of])
        else:
            where.append("forgotten_at IS NULL")
        if filters and filters.tags:
            where.append(
                "EXISTS (SELECT 1 FROM json_each(astrocyte_vectors.tags) "
                f"WHERE json_each.value IN ({_placeholders(len(filters.tags))}))"
            )
            params.extend(filters.tags)
        if filters and filters.fact_types:
            where.append(f"fact_type IN ({_placeholders(len(filters.fact_types))})")
            params.extend(filters.fact_types)
        if with_time_range and filters and filters.time_range:
            start, end = filters.time_range
            where.append("occurred_at >= ?")
            where.append("occurred_at <= ?")
            params.extend([_to_us(start), _to_us(end)])
        return where, params

    @staticmethod
    def _row_to_hit(row: sqlite3.Row, score: float) -> VectorHit:
        return VectorHit(
            id=row["id"],
            text=row["text"],
            score=score,
            metadata=_decode_metadata(row["metadata"]),
            tags=_decode_tags(row["tags"]),
            fact_type=row["fact_type"],
            occurred_at=_from_us(row["occurred_at"]),
            memory_layer=row["memory_layer"],
            retained_at=_from_us(row["retained_at"]),
            chunk_id=row["chunk_id"],
        )

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> VectorItem:
        # Mirrors PostgresStore, whose list paths do not return chunk_id.
        return VectorItem(
            id=row["id"],
            bank_id=row["bank_id"],
            vector=_decode_vector(row["embedding"]),
            text=row["text"],
            metadata=_decode_metadata(row["metadata"]),
            tags=_decode_tags(row["tags"]),
            fact_type=row["fact_type"],
            occurred_at=_from_us(row["occurred_at"]),
            memory_layer=row["memory_layer"],
            retained_at=_from_us(row["retained_at"]),
        )

    # ── VectorStore ───────────────────────────────────────────────────

    async def store_vectors(self, items: list[VectorItem]) -> list[str]:
        return await self._run(self._write_vectors, items, True)

    async def insert_vectors(self, items: list[VectorItem]) -> list[str]:
        """Insert-only ``store_vectors``: an item whose id already exists in
        any bank, live or forgotten, is skipped, never overwritten. Returns
        the ids inserted. Optional VectorStore method (team-memory push)."""
        return await self._run(self._write_vectors, items, False)

    def _store_vectors(self, items: list[VectorItem]) -> list[str]:
        return self._write_vectors(items, True)

    def _write_vectors(self, items: list[VectorItem], overwrite: bool) -> list[str]:
        """Upsert (``overwrite``, the reference's semantics: a re-stored id
        takes the new values and is live again) or insert-only."""
        self._ensure_schema()
        if not items:
            return []
        dim = self._dim or len(items[0].vector)
        # Validate the whole batch before writing: a bad item fails the batch
        # atomically, as the reference's single transaction does.
        for item in items:
            if len(item.vector) != dim:
                raise ValueError(f"Vector length {len(item.vector)} != embedding_dimensions {dim}")
        sql = _INSERT_VECTOR + (_ON_CONFLICT_UPSERT if overwrite else "ON CONFLICT(id) DO NOTHING")
        # changed_at: a new row's is its retained_at (?10). Overwriting an
        # existing row (a restore, a metadata rewrite that keeps the old
        # retained_at) is a change now (?12), so the feed shows it after any
        # cursor already handed out.
        now = _now_us()
        written: list[str] = []
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if self._dim is None:
                    # Another process may have recorded a dimension first.
                    row = conn.execute("SELECT value FROM astrocyte_meta WHERE key = 'embedding_dimensions'").fetchone()
                    if row and int(row["value"]) != dim:
                        raise ValueError(f"Vector length {dim} != embedding_dimensions {row['value']}")
                    conn.execute(
                        "INSERT OR IGNORE INTO astrocyte_meta(key, value) VALUES ('embedding_dimensions', ?)",
                        (str(dim),),
                    )
                for item in items:
                    if not overwrite and conn.execute(
                        "SELECT 1 FROM astrocyte_tombstones WHERE id = ?", (item.id,)
                    ).fetchone():
                        continue  # erased: the id stays forgotten
                    params = [
                        item.id,
                        item.bank_id,
                        _encode_vector(item.vector),
                        item.text,
                        json.dumps(item.metadata) if item.metadata is not None else None,
                        _encode_tags(item.tags),
                        item.fact_type,
                        _to_us(item.occurred_at),
                        item.memory_layer,
                        _to_us(item.retained_at) if item.retained_at else now,
                        item.chunk_id,
                    ]
                    if overwrite:
                        params.append(now)  # ?12
                    cur = conn.execute(sql, params)
                    if overwrite or cur.rowcount == 1:
                        written.append(item.id)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        self._dim = dim
        return written

    async def lookup_ids(self, ids: list[str]) -> list[MemoryChange]:
        """The current state of each id held in **any** bank: a live row or a
        tombstone; unknown (or purged) ids are absent. Cross-bank on purpose:
        rows are keyed on id alone, so a writer that must not take over
        another bank's row has to see it. Optional VectorStore method."""
        return await self._run(self._lookup_ids, ids)

    def _lookup_ids(self, ids: list[str]) -> list[MemoryChange]:
        wanted = list(dict.fromkeys(ids))
        if not wanted:
            return []
        self._ensure_schema()
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_CHANGE_COLUMNS} FROM astrocyte_vectors WHERE id IN ({_placeholders(len(wanted))})",
                wanted,
            ).fetchall()
            erased = conn.execute(
                f"SELECT id, bank_id, changed_at FROM astrocyte_tombstones WHERE id IN ({_placeholders(len(wanted))})",
                wanted,
            ).fetchall()
        finally:
            conn.close()
        by_id = {r["id"]: self._tombstone(r) for r in erased}
        by_id.update({r["id"]: self._row_to_change(r) for r in rows})
        return [by_id[i] for i in wanted if i in by_id]

    async def search_similar(
        self,
        query_vector: list[float],
        bank_id: str,
        limit: int = 10,
        filters: VectorFilters | None = None,
    ) -> list[VectorHit]:
        return await self._run(self._search_similar, query_vector, bank_id, limit, filters)

    def _search_similar(
        self,
        query_vector: list[float],
        bank_id: str,
        limit: int,
        filters: VectorFilters | None,
    ) -> list[VectorHit]:
        self._ensure_schema()
        self._check_dim(len(query_vector), "Query vector")
        if limit <= 0:
            return []
        where, params = self._live_filters(filters, with_time_range=False)
        conn = self._connect()
        try:
            # Phase 1: score only (pk, embedding). Phase 2: hydrate the winners.
            rows = conn.execute(
                f"SELECT pk, embedding FROM astrocyte_vectors WHERE bank_id = ? AND {' AND '.join(where)}",
                [bank_id, *params],
            ).fetchall()
            if not rows:
                return []
            pks = np.fromiter((r["pk"] for r in rows), dtype=np.int64, count=len(rows))
            matrix = np.frombuffer(b"".join(r["embedding"] for r in rows), dtype=np.float32)
            matrix = matrix.reshape(len(rows), -1)
            # Rank on the raw cosine and clamp only the reported score — the
            # reference ORDER BYs the unclamped distance, so every negatively
            # correlated candidate (all reported as 0.0) still has a true order.
            raw = _cosine_raw(np.asarray(query_vector, dtype=np.float32), matrix)
            rank = {int(pks[i]): float(raw[i]) for i in _top_k(raw, limit)}
            hydrated = conn.execute(
                f"SELECT pk, {_HIT_COLUMNS} FROM astrocyte_vectors WHERE pk IN ({_placeholders(len(rank))})",
                list(rank),
            ).fetchall()
        finally:
            conn.close()
        # Best raw similarity first; id breaks exact ties deterministically
        # (the reference leaves tie order to the planner).
        hydrated.sort(key=lambda r: (-rank[r["pk"]], r["id"]))
        return [self._row_to_hit(r, _clamp(rank[r["pk"]])) for r in hydrated]

    async def delete(self, ids: list[str], bank_id: str) -> int:
        return await self._run(self._delete, ids, bank_id)

    def _delete(self, ids: list[str], bank_id: str) -> int:
        """Soft delete — sets ``forgotten_at`` so ``as_of`` time travel still
        sees the memory as it was, exactly like the reference."""
        if not ids:
            return 0
        self._ensure_schema()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # A forget is a change: the tombstone sorts into the feed at
            # max(retained_at, forgotten_at), i.e. when it was made.
            cur = conn.execute(
                f"UPDATE astrocyte_vectors SET forgotten_at = ?1, changed_at = MAX(retained_at, ?1) "
                f"WHERE bank_id = ?2 AND forgotten_at IS NULL "
                f"AND id IN ({_placeholders(len(ids))})",
                [_now_us(), bank_id, *ids],
            )
            conn.execute("COMMIT")
            return cur.rowcount or 0
        finally:
            conn.close()

    async def purge(self, bank_id: str, ids: list[str] | None = None) -> int:
        """Erase forgotten memories from disk; returns how many were erased.

        ``delete`` is a soft delete (the reference's semantics: ``as_of``
        still sees the past), so a forgotten memory's text stays in the file.
        When a user removes something — a pasted key, a remark they regret —
        it has to actually go. Only already-forgotten rows are erased (all of
        the bank's, or those of ``ids``), so purge never bypasses forget.

        Not part of the VectorStore SPI; ``astrocyte memory forget`` calls it
        where available. FTS entries are removed outright (FTS5
        ``secure-delete``, SQLite 3.42+), the file is rebuilt (``VACUUM``,
        with ``secure_delete``) and the WAL checkpointed, so no copy survives
        in the database files. The rebuild is O(store size): seconds for a
        local store, which is the trade for an erase that is one.
        """
        return await self._run(self._purge, bank_id, ids)

    async def erase(self, bank_id: str, ids: list[str]) -> int:
        """Erase forgotten memories from disk but keep their tombstones;
        returns how many were erased.

        Optional VectorStore method: what a gateway's erase calls (a DSAR, a
        team forget). Unlike :meth:`purge`, the change feed still serves each
        id as deleted, so mirrors erase it too, and the id is never stored
        again (``insert_vectors`` skips it). Only already-forgotten rows of
        ``ids`` are erased, so erase never bypasses forget.
        """
        return await self._run(self._purge, bank_id, list(ids), True)

    def _purge(self, bank_id: str, ids: list[str] | None, keep_tombstones: bool = False) -> int:
        self._ensure_schema()
        conn = self._connect()
        try:
            conn.execute("PRAGMA secure_delete = ON")
            if self._fts:
                try:
                    conn.execute(
                        "INSERT INTO astrocyte_vectors_fts(astrocyte_vectors_fts, rank) VALUES('secure-delete', 1)"
                    )
                except sqlite3.OperationalError:
                    pass  # older SQLite: deleted terms linger in the index until segments merge
            sql = "DELETE FROM astrocyte_vectors WHERE bank_id = ? AND forgotten_at IS NOT NULL"
            params: list[Any] = [bank_id]
            if ids is not None:
                if not ids:
                    return 0
                sql += f" AND id IN ({_placeholders(len(ids))})"
                params += ids
            conn.execute("BEGIN IMMEDIATE")
            if keep_tombstones:
                conn.execute(
                    "INSERT OR IGNORE INTO astrocyte_tombstones (id, bank_id, changed_at) "
                    "SELECT id, bank_id, COALESCE(changed_at, forgotten_at) FROM astrocyte_vectors"
                    + sql.removeprefix("DELETE FROM astrocyte_vectors"),
                    params,
                )
            erased = conn.execute(sql, params).rowcount or 0
            conn.execute("COMMIT")
            if erased:
                # The soft delete rewrote the row without secure_delete,
                # leaving its old bytes in the page's free space; only a
                # rebuild is sure to drop every stale copy.
                conn.execute("VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return erased
        finally:
            conn.close()

    async def list_vectors(self, bank_id: str, offset: int = 0, limit: int = 100) -> list[VectorItem]:
        return await self._run(self._list_vectors, bank_id, offset, limit)

    def _list_vectors(self, bank_id: str, offset: int, limit: int) -> list[VectorItem]:
        self._ensure_schema()
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM astrocyte_vectors "
                "WHERE bank_id = ? AND forgotten_at IS NULL "
                "ORDER BY id LIMIT ? OFFSET ?",
                (bank_id, limit, offset),
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_item(r) for r in rows]

    async def list_recent_vectors(
        self, bank_id: str, limit: int = 100, filters: VectorFilters | None = None
    ) -> list[VectorItem]:
        return await self._run(self._list_recent_vectors, bank_id, limit, filters)

    def _list_recent_vectors(self, bank_id: str, limit: int, filters: VectorFilters | None) -> list[VectorItem]:
        self._ensure_schema()
        where, params = self._live_filters(filters, with_time_range=True)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM astrocyte_vectors "
                f"WHERE bank_id = ? AND {' AND '.join(where)} "
                "ORDER BY COALESCE(occurred_at, retained_at) DESC, id LIMIT ?",
                [bank_id, *params, limit],
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_item(r) for r in rows]

    async def list_changes(
        self,
        bank_id: str,
        *,
        after: tuple[datetime, str] | None = None,
        limit: int = 100,
    ) -> list[MemoryChange]:
        """The bank's change feed — every change to a synced row: live rows
        (current values) and tombstones (forgotten rows), ordered by
        ``(changed_at, id)``, strictly after ``after``. ``changed_at`` is the
        row's last change to any synced field; every write sets it.

        Optional VectorStore method (team-memory sync). An erased row
        (:meth:`erase`) is still served as a tombstone; a purged one
        (:meth:`purge`, the local store's own erase) is gone.
        """
        return await self._run(self._list_changes, bank_id, after, limit)

    def _list_changes(self, bank_id: str, after: tuple[datetime, str] | None, limit: int) -> list[MemoryChange]:
        self._ensure_schema()
        if limit <= 0:
            return []
        where, params = "bank_id = ?", [bank_id]
        if after is not None:
            # Row values compare lexicographically; ids compare as BINARY,
            # which matches Postgres's COLLATE "C".
            where += " AND (changed_at, id) > (?, ?)"
            params += [_to_us(after[0]), after[1]]
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_CHANGE_COLUMNS} FROM astrocyte_vectors WHERE {where} ORDER BY changed_at, id LIMIT ?",
                [*params, limit],
            ).fetchall()
            erased = conn.execute(
                f"SELECT id, bank_id, changed_at FROM astrocyte_tombstones WHERE {where} "
                "ORDER BY changed_at, id LIMIT ?",
                [*params, limit],
            ).fetchall()
        finally:
            conn.close()
        # Two ordered runs merged: the first ``limit`` of both is the first ``limit`` of the union.
        changes = [self._row_to_change(r) for r in rows] + [self._tombstone(r) for r in erased]
        changes.sort(key=lambda c: (c.changed_at, c.id))
        return changes[:limit]

    @staticmethod
    def _tombstone(row: sqlite3.Row) -> MemoryChange:
        from astrocyte.types import MemoryChange

        return MemoryChange(id=row["id"], bank_id=row["bank_id"], changed_at=_from_us(row["changed_at"]), deleted=True)

    @staticmethod
    def _row_to_change(row: sqlite3.Row) -> MemoryChange:
        from astrocyte.types import MemoryChange

        # Every write sets changed_at and the upgrade backfills it.
        changed_at = _from_us(row["changed_at"])
        if changed_at is None:  # pragma: no cover — defensive
            changed_at = _EPOCH + timedelta(microseconds=row["retained_at"])
        if row["forgotten_at"] is not None:
            return MemoryChange(id=row["id"], bank_id=row["bank_id"], changed_at=changed_at, deleted=True)
        return MemoryChange(
            id=row["id"],
            bank_id=row["bank_id"],
            changed_at=changed_at,
            text=row["text"],
            occurred_at=_from_us(row["occurred_at"]),
            retained_at=_from_us(row["retained_at"]),
            tags=_decode_tags(row["tags"]),
            fact_type=row["fact_type"],
            memory_layer=row["memory_layer"],
            metadata=_decode_metadata(row["metadata"]),
        )

    async def list_banks(self) -> list[tuple[str, int, datetime | None]]:
        """Every bank holding live memories: ``(bank_id, count, newest)``.

        Not part of the VectorStore SPI; ``astrocyte memory banks`` uses it
        where available, since a local store has no other bank registry.
        """
        return await self._run(self._list_banks)

    def _list_banks(self) -> list[tuple[str, int, datetime | None]]:
        self._ensure_schema()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT bank_id, COUNT(*), MAX(COALESCE(occurred_at, retained_at)) FROM astrocyte_vectors "
                "WHERE forgotten_at IS NULL GROUP BY bank_id ORDER BY 3 DESC"
            ).fetchall()
        finally:
            conn.close()
        return [(r[0], r[1], _from_us(r[2])) for r in rows]

    async def get_by_chunk_ids(self, chunk_ids: list[str], bank_id: str) -> list[VectorHit]:
        return await self._run(self._get_by_chunk_ids, chunk_ids, bank_id)

    def _get_by_chunk_ids(self, chunk_ids: list[str], bank_id: str) -> list[VectorHit]:
        if not chunk_ids:
            return []
        self._ensure_schema()
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT {_HIT_COLUMNS} FROM astrocyte_vectors "
                "WHERE bank_id = ? AND forgotten_at IS NULL "
                f"AND chunk_id IN ({_placeholders(len(chunk_ids))}) ORDER BY id",
                [bank_id, *chunk_ids],
            ).fetchall()
        finally:
            conn.close()
        return [self._row_to_hit(r, 1.0) for r in rows]

    async def close(self) -> None:
        """No pooled resources: every call opens and closes its own connection."""

    async def health(self) -> HealthStatus:
        return await self._run(self._health)

    def _health(self) -> HealthStatus:
        started = time.perf_counter()
        try:
            self._ensure_schema()
            conn = self._connect()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 — health reports, never raises
            return HealthStatus(healthy=False, message=f"sqlite unhealthy ({self._path}): {e!s}")
        mode = "fts5" if self._fts else "LIKE fallback (no FTS5)"
        return HealthStatus(
            healthy=True,
            message=f"sqlite {self._path} — keyword search: {mode}",
            latency_ms=(time.perf_counter() - started) * 1000,
            last_check_at=datetime.now(UTC),
        )

    # ── DocumentStore ─────────────────────────────────────────────────
    # Same arrangement as PostgresStore: memory text is already stored by
    # store_vectors(), and the FTS5 triggers keep the index in sync, so the
    # DocumentStore methods read the same table.

    async def store_document(self, document: Document, bank_id: str) -> str:
        """No-op: text is indexed at retain time by store_vectors()."""
        return document.id

    async def search_fulltext(
        self,
        query: str,
        bank_id: str,
        limit: int = 10,
        filters: DocumentFilters | None = None,
    ) -> list[DocumentHit]:
        return await self._run(self._search_fulltext, query, bank_id, limit, filters)

    def _search_fulltext(
        self,
        query: str,
        bank_id: str,
        limit: int,
        filters: DocumentFilters | None,
    ) -> list[DocumentHit]:
        if not query or not query.strip():
            return []
        terms = _keyword_terms(query)
        if not terms:
            return []  # all stopwords: plainto_tsquery yields an empty query
        self._ensure_schema()
        tag_sql, tag_params = "", []
        if filters and filters.tags:
            tag_sql = (
                " AND EXISTS (SELECT 1 FROM json_each(v.tags) "
                f"WHERE json_each.value IN ({_placeholders(len(filters.tags))}))"
            )
            tag_params = list(filters.tags)
        conn = self._connect()
        try:
            if self._fts:
                match = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
                rows = conn.execute(
                    "SELECT v.id, v.text, v.metadata, -bm25(astrocyte_vectors_fts) AS score "
                    "FROM astrocyte_vectors_fts "
                    "JOIN astrocyte_vectors v ON v.pk = astrocyte_vectors_fts.rowid "
                    "WHERE astrocyte_vectors_fts MATCH ? AND v.bank_id = ? "
                    f"AND v.forgotten_at IS NULL{tag_sql} "
                    "ORDER BY score DESC, v.id LIMIT ?",
                    [match, bank_id, *tag_params, limit],
                ).fetchall()
            else:
                rows = self._like_search(conn, terms, bank_id, tag_sql, tag_params, limit)
        finally:
            conn.close()
        return [
            DocumentHit(
                document_id=r["id"],
                text=r["text"],
                score=float(r["score"]),
                metadata=_decode_metadata(r["metadata"]),
            )
            for r in rows
        ]

    @staticmethod
    def _like_search(
        conn: sqlite3.Connection,
        terms: list[str],
        bank_id: str,
        tag_sql: str,
        tag_params: list[Any],
        limit: int,
    ) -> list[sqlite3.Row]:
        """Degraded keyword search for SQLite builds without FTS5: every term
        must appear (no stemming); ranked by matched-term density."""
        likes = " AND ".join("lower(v.text) LIKE ?" for _ in terms)
        score = " + ".join("(length(lower(v.text)) - length(replace(lower(v.text), ?, ''))) / length(?)" for _ in terms)
        score_params: list[Any] = []
        for t in terms:
            score_params.extend([t, t])
        return conn.execute(
            f"SELECT v.id, v.text, v.metadata, "
            f"CAST(({score}) AS REAL) / (length(v.text) + 1) AS score "
            f"FROM astrocyte_vectors v WHERE v.bank_id = ? AND v.forgotten_at IS NULL "
            f"AND {likes}{tag_sql} ORDER BY score DESC, v.id LIMIT ?",
            [*score_params, bank_id, *(f"%{t}%" for t in terms), *tag_params, limit],
        ).fetchall()

    async def get_document(self, document_id: str, bank_id: str) -> Document | None:
        return await self._run(self._get_document, document_id, bank_id)

    def _get_document(self, document_id: str, bank_id: str) -> Document | None:
        self._ensure_schema()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, text, metadata, tags FROM astrocyte_vectors "
                "WHERE id = ? AND bank_id = ? AND forgotten_at IS NULL",
                (document_id, bank_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return Document(
            id=row["id"],
            text=row["text"],
            metadata=_decode_metadata(row["metadata"]),
            tags=_decode_tags(row["tags"]),
        )


# ── numerics ─────────────────────────────────────────────────────────────


# Below any real cosine (>= -1), so zero-norm rows rank last — where pgvector's
# NaN distance puts them under ORDER BY.
_UNRANKABLE = -2.0


def _cosine_raw(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Unclamped cosine similarity, i.e. ``1 - cosine_distance``.

    A zero vector has no direction: pgvector returns NaN distance for it, which
    sorts last. Those rows (and every row, for a zero query) get
    ``_UNRANKABLE`` and report a score of 0.0 rather than NaN.
    """
    q_norm = float(np.linalg.norm(query))
    if q_norm == 0.0:
        return np.full(matrix.shape[0], _UNRANKABLE, dtype=np.float64)
    wide = matrix.astype(np.float64)
    row_norms = np.linalg.norm(wide, axis=1)
    dots = wide @ query.astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(row_norms > 0, dots / (row_norms * q_norm), _UNRANKABLE)


def _clamp(raw: float) -> float:
    """The reported score: the reference clamps ``1 - distance`` to [0, 1]."""
    return min(max(raw, 0.0), 1.0)


def _top_k(scores: np.ndarray, k: int) -> Iterable[int]:
    if k >= scores.shape[0]:
        return range(scores.shape[0])
    return np.argpartition(-scores, k - 1)[:k].tolist()
