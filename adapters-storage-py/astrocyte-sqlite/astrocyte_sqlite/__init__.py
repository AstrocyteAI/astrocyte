"""SQLite adapter for Astrocyte — zero-infrastructure local storage.

One file on disk, no server, no native extensions. Provides a
:class:`SqliteStore` satisfying both the VectorStore and DocumentStore
protocols, with semantics matched to :class:`astrocyte_postgres.PostgresStore`
so recall behaves the same on a laptop as on the benched production backend.
"""

from astrocyte_sqlite.store import SqliteStore

__all__ = ["SqliteStore"]
