"""Schema-per-tenant primitives.

Astrocyte supports tenant isolation at the **PostgreSQL schema** level. Each
tenant gets a dedicated schema (e.g. ``tenant_acme``); every adapter SQL string
that references a table goes through :func:`fq_table` so the table reference
gets prefixed with the active tenant's schema name.

The active schema for a request is set via ``contextvars.ContextVar``, which
gives us automatic per-async-task isolation: concurrent requests for different
tenants never see each other's schema, and no thread-locals or manual passing
through every function are required.

## Public surface

- :class:`TenantContext`, :class:`Tenant`, :class:`TenantExtension`,
  :class:`DefaultTenantExtension` — the pluggable auth contract.
  Implement :class:`TenantExtension` to map an inbound request to a schema.
- :func:`get_current_schema` — the active schema (defaults to ``"public"``).
- :func:`fq_table` — table-name helper used by every adapter SQL string.
- :func:`use_schema` — context manager / decorator helper for binding the
  schema for a block of code (used by gateway middleware and workers).

## Design rationale

- **ContextVar over thread-local**: we run async; ContextVar is the correct
  primitive and propagates across ``asyncio.create_task`` automatically.
- **Default schema = ``"public"``**: existing single-schema deployments keep
  working without code changes. Schema-per-tenant is opt-in via a custom
  :class:`TenantExtension`.
- **No global mutable state besides the ContextVar**: tests can call
  ``use_schema("test_xyz")`` to scope a block.
- **Identifier validation in :func:`fq_table`**: schema and table names are
  validated against a strict regex (alphanumeric + underscore). Anything
  else raises ``ValueError``. This is the *only* defense against SQL injection
  through the schema name — there is no way to safely parameterize an
  identifier in PostgreSQL.
"""

from __future__ import annotations

import contextvars
import re
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Identifier validation
# ---------------------------------------------------------------------------

#: Regex allowed for schema and table names. Postgres unquoted identifiers
#: tolerate a wider set, but we restrict to ``[a-zA-Z_][a-zA-Z0-9_]*`` so
#: identifiers can never need quoting and SQL-injection through this surface
#: is structurally impossible.
_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

#: Default schema when no tenant context is active. Single-schema deployments
#: never need to set anything; multi-tenant deployments override this per
#: request via :func:`use_schema` or by setting :data:`_current_schema`.
DEFAULT_SCHEMA = "public"


def _validate_identifier(value: str, *, label: str) -> str:
    """Validate an identifier against the safe-character regex.

    Raises ``ValueError`` if the identifier contains anything that would need
    quoting. This is the SQL-injection guard — never relax it.
    """
    if not isinstance(value, str) or not _IDENT_RE.match(value):
        raise ValueError(
            f"{label} {value!r} is not a valid PostgreSQL identifier "
            f"(must match {_IDENT_RE.pattern})"
        )
    return value


# ---------------------------------------------------------------------------
# Schema context (per-request, propagates across asyncio.create_task)
# ---------------------------------------------------------------------------

#: Per-request active schema. ``None`` means "use :data:`DEFAULT_SCHEMA`".
_current_schema: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "astrocyte.tenancy.current_schema",
    default=None,
)


def get_current_schema() -> str:
    """Return the active schema name (defaults to :data:`DEFAULT_SCHEMA`)."""
    return _current_schema.get() or DEFAULT_SCHEMA


def set_current_schema(schema: str) -> contextvars.Token:
    """Bind ``schema`` as the active schema and return a token for reset.

    Prefer :func:`use_schema` (context manager) when possible — it can't
    forget to reset. ``set_current_schema`` is provided for middleware that
    needs to bind manually around try/finally.
    """
    _validate_identifier(schema, label="schema")
    return _current_schema.set(schema)


def reset_current_schema(token: contextvars.Token) -> None:
    """Restore the previous schema after :func:`set_current_schema`."""
    _current_schema.reset(token)


@contextmanager
def use_schema(schema: str):
    """Bind ``schema`` as the active schema for the lifetime of the block.

    Example::

        with use_schema("tenant_acme"):
            await store.search_similar(...)
    """
    token = set_current_schema(schema)
    try:
        yield
    finally:
        reset_current_schema(token)


# ---------------------------------------------------------------------------
# Fully-qualified table-name helper
# ---------------------------------------------------------------------------


def fq_table(table_name: str, *, schema: str | None = None) -> str:
    """Return ``"<schema>"."<table>"`` using the active or explicit schema.

    Every adapter SQL string that references a tenant-scoped table goes
    through this helper. Use the explicit ``schema`` parameter only from
    workers/jobs that don't run in a request context; everything else relies
    on the ContextVar via :func:`get_current_schema`.

    Both schema and table identifiers are validated; anything that would
    require quoting raises :class:`ValueError`. Output is intentionally
    double-quoted on both halves so the result is safe regardless of any
    Postgres reserved-word behaviour.
    """
    schema = schema if schema is not None else get_current_schema()
    _validate_identifier(schema, label="schema")
    _validate_identifier(table_name, label="table")
    return f'"{schema}"."{table_name}"'


def fq_function(function_name: str, *, schema: str | None = None) -> str:
    """Return ``"<schema>"."<function>"`` (for trigger functions, etc.)."""
    schema = schema if schema is not None else get_current_schema()
    _validate_identifier(schema, label="schema")
    _validate_identifier(function_name, label="function")
    return f'"{schema}"."{function_name}"'


# ---------------------------------------------------------------------------
# Tenant extension contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantContext:
    """Result of authenticating a request.

    A tenant context is *just* the schema name. Anything richer (entitlements,
    quotas, feature flags) belongs in higher-level auth/policy layers — this
    is the minimal contract needed for storage isolation.
    """

    schema_name: str


@dataclass(frozen=True)
class Tenant:
    """A tenant the worker should poll for background tasks."""

    schema: str


class AuthenticationError(Exception):
    """Raised by a :class:`TenantExtension` when auth fails."""


class TenantExtension(ABC):
    """Pluggable contract: map a request to a Postgres schema.

    Implementations decide *how* a request maps to a tenant — API key lookup,
    JWT claim, mTLS subject, environment, etc. The only thing the storage
    layer needs back is the schema name to bind for that request.
    """

    @abstractmethod
    async def authenticate(self, context: Any) -> TenantContext:
        """Validate ``context`` and return the schema the request should run in.

        ``context`` is intentionally typed as :class:`Any` because the request
        shape varies across HTTP, MCP, gRPC, in-process, etc. Implementations
        should accept whatever their transport layer provides and produce a
        :class:`TenantContext`.

        Raises :class:`AuthenticationError` on failure.
        """

    @abstractmethod
    async def list_tenants(self) -> list[Tenant]:
        """List all tenants whose schemas should be polled by background workers.

        Single-tenant deployments return ``[Tenant(schema=DEFAULT_SCHEMA)]``.
        """


class DefaultTenantExtension(TenantExtension):
    """Single-tenant default: no authentication, fixed schema.

    Use this when you don't need tenant isolation. It returns the same schema
    for every request. ``schema`` defaults to :data:`DEFAULT_SCHEMA` (``"public"``)
    so existing single-schema deployments keep working without configuration.

    For real multi-tenant setups, write a custom :class:`TenantExtension` that
    looks up the schema for each request (e.g., from an API-key table or JWT
    claim) and configure it via the gateway's tenant-extension hook.
    """

    def __init__(self, schema: str = DEFAULT_SCHEMA) -> None:
        _validate_identifier(schema, label="schema")
        self._schema = schema

    async def authenticate(self, context: Any) -> TenantContext:  # noqa: ARG002 — context unused
        return TenantContext(schema_name=self._schema)

    async def list_tenants(self) -> list[Tenant]:
        return [Tenant(schema=self._schema)]


__all__ = [
    "AuthenticationError",
    "DEFAULT_SCHEMA",
    "DefaultTenantExtension",
    "Tenant",
    "TenantContext",
    "TenantExtension",
    "fq_function",
    "fq_table",
    "get_current_schema",
    "reset_current_schema",
    "set_current_schema",
    "use_schema",
]
