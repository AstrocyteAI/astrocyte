"""Reset all benchmark-relevant Postgres state before a run.

Why this exists
---------------

Benchmarks must start from an identical clean state every run, otherwise
score deltas reflect leftover state instead of the change under test.

The per-bank ``clean_after=True`` path inside each benchmark only deletes
``astrocyte_vectors`` rows for one ``bank_id``.  It does NOT clear:

- ``astrocyte_wiki_*``         — stale persona pages would dominate ``_try_wiki_tier``
- ``astrocyte_entities*``      — accumulated aliases corrupt canonical-id resolution
- ``astrocyte_temporal_facts`` — phantom temporal hits across runs
- ``pgqueuer*``                — orphaned compile tasks race the recall path
- AGE graph nodes/edges        — phantom multi-hop paths

This helper TRUNCATEs every table the bench code path writes to, then
drops + recreates the AGE graph, and clears the PgQueuer queue.  It does
NOT recreate schema (migrations are idempotent and run before
``bench-db-start``).

If ``DATABASE_URL`` is unset (e.g. ``bench-smoke`` with the in-memory
provider), this is a no-op.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

logger = logging.getLogger("astrocyte.eval.state_reset")

# Ordered for clarity (CASCADE makes ordering mostly cosmetic).
# Children → parents.  Bank metadata is intentionally cleared last so banks
# are recreated on next retain rather than carrying stale ``updated_at``.
_BENCH_TABLES: tuple[str, ...] = (
    # Wiki layer
    "astrocyte_wiki_lint_issues",
    "astrocyte_wiki_revision_sources",
    "astrocyte_wiki_links",
    "astrocyte_wiki_revisions",
    "astrocyte_wiki_pages",
    # Entity layer (cross-store: same tables shared by postgres + AGE)
    "astrocyte_memory_entities",
    "astrocyte_age_mem_entity",
    "astrocyte_entity_links",
    "astrocyte_entity_aliases",
    "astrocyte_entities",
    # Temporal facts
    "astrocyte_temporal_facts",
    # Core vectors + bank metadata
    "astrocyte_vectors",
    "astrocyte_bank_access_grants",
    "astrocyte_banks",
    # PgQueuer
    "pgqueuer",
    "pgqueuer_log",
    "pgqueuer_statistics",
    "pgqueuer_schedules",
)


async def reset_benchmark_state(
    *,
    dsn: str | None = None,
    reset_age_graph: bool = True,
    age_graph_name: str = "astrocyte",
    extra_tables: Iterable[str] = (),
) -> None:
    """Wipe every table a benchmark run writes to, leaving schema intact.

    This is the *pre-run* reset.  Per-bank ``clean_after=True`` is a softer
    cleanup that runs at the *end* of one benchmark; this helper guarantees
    the *start* of the next run is identical regardless of what ran before.

    Args:
        dsn: Postgres DSN.  Defaults to ``DATABASE_URL`` env var.  When
            unset, the helper is a no-op (in-memory test provider path).
        reset_age_graph: When ``True`` (default), drop + recreate the AGE
            graph.  Silently skipped when AGE functions are not installed.
        age_graph_name: AGE graph name; matches ``002_graph.sql`` migration.
        extra_tables: Additional tables to truncate (e.g. project-specific
            extensions registered outside the canonical adapter set).
    """
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        logger.debug("reset_benchmark_state: DATABASE_URL unset, skipping")
        return

    try:
        import psycopg
    except ImportError:
        logger.warning("reset_benchmark_state: psycopg not available, skipping")
        return

    tables = list(_BENCH_TABLES) + list(extra_tables)
    truncated: list[str] = []
    skipped: list[tuple[str, str]] = []

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        for table in tables:
            try:
                async with conn.cursor() as cur:
                    await cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
                truncated.append(table)
            except psycopg.errors.UndefinedTable:
                # Table not present in this deployment (e.g. PgQueuer not
                # installed, wiki layer not migrated).  Safe to skip.
                skipped.append((table, "missing"))
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                skipped.append((table, f"{type(exc).__name__}: {exc!s}"[:120]))

        if reset_age_graph:
            try:
                async with conn.cursor() as cur:
                    # AGE functions live in the ag_catalog schema.  Use
                    # fully-qualified names so we don't depend on
                    # search_path being set on this admin connection.
                    await cur.execute("LOAD 'age'")
                    await cur.execute(
                        "SELECT ag_catalog.drop_graph(%s, true)",
                        [age_graph_name],
                    )
                    await cur.execute(
                        "SELECT ag_catalog.create_graph(%s)",
                        [age_graph_name],
                    )
                truncated.append(f"AGE graph '{age_graph_name}'")
            except psycopg.errors.UndefinedFile:
                # ``LOAD 'age'`` failed: AGE not installed.
                skipped.append(("AGE graph", "AGE extension not installed"))
            except psycopg.errors.UndefinedFunction:
                skipped.append(("AGE graph", "AGE functions not in ag_catalog"))
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                skipped.append((f"AGE graph '{age_graph_name}'", f"{type(exc).__name__}: {exc!s}"[:120]))

    logger.info(
        "reset_benchmark_state: truncated=%d skipped=%d",
        len(truncated),
        len(skipped),
    )
    for name, reason in skipped:
        logger.debug("  skipped %s: %s", name, reason)
