---
title: Schema-per-tenant
---

# Schema-per-tenant

Astrocyte supports tenant isolation at the **PostgreSQL schema** level. Each
tenant gets a dedicated schema (e.g. `tenant_acme`); every storage adapter
(`astrocyte-postgres`, `astrocyte-age`) qualifies its SQL with that schema
on every statement, and Apache AGE graphs are namespaced per-tenant. The
result: cryptographic-strength isolation between tenants, with `pg_dump`
per-tenant, optional Row-Level Security, and zero accidental cross-tenant
leak risk from missing `WHERE bank_id = ?` clauses in application code.

## When to use this vs `bank_id`

Astrocyte already supports multi-tenancy via the `bank_id` column on every
row. Schema-per-tenant is a **strictly stronger** isolation model — choose
it when:

- **Compliance demands hard isolation** (HIPAA, SOC2, EU data residency)
- **Per-tenant `pg_dump` / restore is a real workflow**
- **Tenants are few enough to fit in Postgres' catalog** (≲ 1000)
- **You want per-tenant performance metrics, schema versions, or extensions**

Stick with `bank_id` when:

- **Tenants are very many** (10k+) — schema-per-tenant bloats `pg_class`
- **Cross-tenant analytics queries are common** (UNION across schemas is awkward)
- **You're early-stage and don't need the operational complexity yet**

## Default behavior is unchanged

If you don't configure a tenant extension, **everything routes to the
`public` schema** — exactly the single-schema behavior every existing
deployment already has. The `astrocyte.tenancy.DEFAULT_SCHEMA` constant is
`"public"`, and every adapter resolves its schema via
`astrocyte.tenancy.get_current_schema()`, which returns the default when
no `use_schema()` block or middleware has bound an active schema.

This is the test that locks in the invariant:

```python
def test_default_schema_is_public(self):
    assert get_current_schema() == "public"
    assert fq_table("astrocyte_vectors") == '"public"."astrocyte_vectors"'
```

So adopting schema-per-tenant is **opt-in** at the gateway configuration
boundary. Existing deployments need no migration, no config change, and no
behavior change.

## Architecture

### The four moving parts

```
┌─────────────────────────────────────────────────────────────────┐
│  HTTP request                                                    │
│       │                                                          │
│       ▼                                                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Gateway middleware (astrocyte_gateway.tenancy)           │   │
│  │   tenant_extension.authenticate(request)                 │   │
│  │   → TenantContext(schema_name="tenant_acme")             │   │
│  │   → set_current_schema("tenant_acme")                    │   │
│  └──────────────────────────────────────────────────────────┘   │
│       │                                                          │
│       ▼                                                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Endpoint handler (recall, retain, wiki, etc.)            │   │
│  │   await store.search_similar(...)                        │   │
│  └──────────────────────────────────────────────────────────┘   │
│       │                                                          │
│       ▼                                                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Adapter SQL (astrocyte-postgres / astrocyte-age)         │   │
│  │   "SELECT ... FROM " + fq_table("astrocyte_vectors")     │   │
│  │   → "SELECT ... FROM \"tenant_acme\".astrocyte_vectors"  │   │
│  └──────────────────────────────────────────────────────────┘   │
│       │                                                          │
│       ▼                                                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ PostgreSQL schema "tenant_acme"                          │   │
│  │   astrocyte_vectors, astrocyte_wiki_*,                   │   │
│  │   astrocyte_entities, ...                                │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

In a separate background loop:

```
┌─────────────────────────────────────────────────────────────────┐
│  Worker startup (astrocyte_gateway.tasks)                        │
│       │                                                          │
│       ▼                                                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ tenants = await tenant_extension.list_tenants()          │   │
│  │ for tenant in tenants:                                   │   │
│  │   conn = await psycopg.AsyncConnection.connect(dsn)      │   │
│  │   await conn.execute('SET search_path = "<schema>"...')  │   │
│  │   spawn worker_task(use_schema(tenant.schema))           │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Key primitives

All in `astrocyte.tenancy`:

| Primitive | Role |
|---|---|
| `_current_schema` | `ContextVar[str \| None]` — per-request active schema |
| `get_current_schema() -> str` | Returns active schema or `"public"` default |
| `use_schema(schema)` | Context manager that binds the schema for a block |
| `fq_table(name)` | Returns `"<schema>"."<table>"` (with identifier validation) |
| `fq_function(name)` | Same, for trigger functions |
| `TenantContext(schema_name)` | What `authenticate()` returns |
| `Tenant(schema)` | What `list_tenants()` returns |
| `TenantExtension` (ABC) | Pluggable auth contract |
| `DefaultTenantExtension` | Single-tenant default — reads `ASTROCYTE_DATABASE_SCHEMA` |
| `AuthenticationError` | Raised by `authenticate()` to produce HTTP 401 |

### SQL-injection guard

Every schema and table identifier is validated against
`^[a-zA-Z_][a-zA-Z0-9_]*$` before being interpolated into SQL. Anything
else raises `ValueError`. This is the **only** defense — Postgres has no
parameterizable identifiers — so the regex is intentionally strict
(stricter than Postgres allows for unquoted identifiers).

The validation runs at three layers:

1. `set_current_schema()` and `use_schema()` validate the schema before binding.
2. `fq_table()` and `fq_function()` validate both schema and identifier on every call.
3. `migrate.sh` and `migrate-all-tenants.sh` validate `SCHEMA=` / `--tenants` arguments before any psql invocation.

Tested by 30+ negative cases in `tests/test_tenancy.py`, including
classic injection shapes (`'; DROP TABLE x; --`), null bytes, leading
digits, hyphens, dots, and Unicode.

## Setup: how to enable it

### Step 1: write a custom `TenantExtension`

The shipped `DefaultTenantExtension` returns a single fixed schema. For
real multi-tenancy, write an extension that maps an inbound request to a
schema. The contract is one method:

```python
from astrocyte.tenancy import (
    AuthenticationError,
    Tenant,
    TenantContext,
    TenantExtension,
)


class ApiKeyTenantExtension(TenantExtension):
    """Look up the tenant schema for an inbound request via API key."""

    def __init__(self, key_to_schema: dict[str, str]) -> None:
        self._key_to_schema = key_to_schema

    async def authenticate(self, request) -> TenantContext:
        # `request` is the FastAPI Request object — inspect any header
        # / query param / body field you need.
        api_key = request.headers.get("X-Api-Key")
        if not api_key:
            raise AuthenticationError("missing X-Api-Key header")
        schema = self._key_to_schema.get(api_key)
        if not schema:
            raise AuthenticationError("unknown API key")
        return TenantContext(schema_name=schema)

    async def list_tenants(self) -> list[Tenant]:
        # Used by the worker to fan out one async-task poller per tenant.
        return [Tenant(schema=s) for s in sorted(set(self._key_to_schema.values()))]
```

For production you'd back `_key_to_schema` with a database lookup or JWT
claim resolution; the contract doesn't care.

### Step 2: pass the extension to `create_app`

```python
from astrocyte_gateway.app import create_app

extension = ApiKeyTenantExtension(load_key_directory_from_secrets())
app = create_app(tenant_extension=extension)
```

That's it. The middleware runs `await extension.authenticate(request)` on
every inbound request (except `/live`, `/health`, `/metrics` which bypass
auth so k8s probes don't 401), binds `_current_schema` for the lifetime
of the request handler, and resets it on the way out — even if the
handler raises.

### Step 3: provision schemas with migrations

For each tenant you want to onboard, create a schema and run migrations
against it:

```bash
# Single tenant
SCHEMA=tenant_acme ./adapters-storage-py/astrocyte-postgres/scripts/migrate.sh

# Many tenants at once
./adapters-storage-py/astrocyte-postgres/scripts/migrate-all-tenants.sh \
    --tenants tenant_acme,tenant_globex,tenant_initech

# Or from a tenants.txt file (one per line, # comments allowed)
./adapters-storage-py/astrocyte-postgres/scripts/migrate-all-tenants.sh \
    --tenants-file infra/tenants.txt

# Or auto-discover all schemas already containing astrocyte_vectors
# (useful for rolling a new migration to every existing tenant)
./adapters-storage-py/astrocyte-postgres/scripts/migrate-all-tenants.sh
```

Or via Make:

```bash
TENANTS=acme,globex make -C astrocyte-services-py migrate-all-tenants
```

## Operations

### Onboarding a new tenant

1. Add the tenant → schema mapping to your `TenantExtension` data source
   (DB row, JWT issuer claim, etc.).
2. Run `SCHEMA=tenant_<name> ./scripts/migrate.sh` to provision the
   schema with all 11 migrations.
3. Restart the gateway so the worker picks up the new tenant via
   `list_tenants()`. (The HTTP path doesn't need a restart — middleware
   re-runs `authenticate()` on every request.)

### Rolling out a new migration

```bash
# Drop migration N+1 into adapters-storage-py/astrocyte-postgres/migrations/
git add adapters-storage-py/astrocyte-postgres/migrations/012_*.sql
git commit -m "schema: add ..."

# Roll forward every existing tenant.
make -C astrocyte-services-py migrate-all-tenants
```

Migrations are idempotent (every CREATE uses `IF NOT EXISTS`, every
ALTER uses `IF NOT EXISTS`), so re-running against schemas that already
have the new migration is a no-op.

### Per-tenant `pg_dump`

```bash
pg_dump --schema=tenant_acme --schema=public dbname > tenant_acme.sql
```

`public` is included because pgvector / pg_trgm / pgcrypto extensions
live there cluster-wide and the dump otherwise references types the
restore can't resolve. The data tables themselves are in the tenant's
schema.

### Worker observability

The gateway logs one INFO line per worker on startup:

```
INFO  astrocyte.gateway.tasks  started pgqueuer worker for tenant schema='tenant_acme'
INFO  astrocyte.gateway.tasks  started pgqueuer worker for tenant schema='tenant_globex'
```

Each worker holds its own pgqueuer connection bound to its tenant's
schema; failures during startup roll back all already-started workers
to avoid leaking connections.

## Apache AGE specifics

AGE graphs are **cluster-global**, not schema-scoped, so the AGE adapter
namespaces them per-tenant by appending `__<schema>` to the base name:

| Schema | Graph name |
|---|---|
| `public` (default) | `astrocyte` (unmangled — backward-compat) |
| `tenant_acme` | `astrocyte__tenant_acme` |
| `tenant_globex` | `astrocyte__tenant_globex` |

The helper SQL tables (`astrocyte_age_mem_entity`, `astrocyte_memory_links`,
`astrocyte_entities`, `astrocyte_entity_aliases`, `astrocyte_entity_links`,
`astrocyte_memory_entities`) live in the tenant's regular Postgres schema
and route through `fq_table()` like everything else.

To drop a tenant's graph:

```sql
LOAD 'age';
SELECT ag_catalog.drop_graph('astrocyte__tenant_acme', true);
```

## Trade-offs vs other approaches

| Concern | Schema-per-tenant | `bank_id` column | Database-per-tenant |
|---|---|---|---|
| Cross-tenant leak risk | ⭐⭐⭐ Cryptographic | ⭐ App-level (one missing WHERE) | ⭐⭐⭐ Cryptographic |
| `pg_dump` per tenant | Trivial (`--schema=`) | Custom export logic | Trivial (`--dbname=`) |
| Cross-tenant analytics | UNION ALL N schemas | Single query | Hard (multi-DB join) |
| Connection pool | Single, `SET search_path` per checkout | Single | One pool per tenant |
| Catalog bloat at 10k+ tenants | Hits Postgres limits | None | Hits Postgres limits sooner |
| Per-tenant Postgres extensions | Possible | Not possible | Possible |
| Migration ops | N runs of `migrate.sh` | One run | N separate DBs |
| Tenant onboarding | New schema + migrations | INSERT INTO banks | New DB + migrations |

Astrocyte chose schema-per-tenant as the **strong-isolation** option that
sits one step above the bank_id model, without the operational complexity
of database-per-tenant. The pluggable `TenantExtension` abstraction means
you can layer additional models (e.g., RLS within a tenant's schema, or
shard tenants across multiple DBs at the connection-pool layer) without
touching the storage code.

## Testing your custom extension

The contract is small enough to fully test without a database:

```python
import pytest
from astrocyte.tenancy import (
    AuthenticationError, Tenant, TenantContext, use_schema, get_current_schema,
)
from fastapi.testclient import TestClient
from astrocyte_gateway.tenancy import install_tenant_middleware
from fastapi import FastAPI


def test_my_extension():
    app = FastAPI()
    install_tenant_middleware(app, MyExtension())

    @app.get("/probe")
    async def probe():
        return {"schema": get_current_schema()}

    client = TestClient(app)
    r = client.get("/probe", headers={"X-Api-Key": "key-acme"})
    assert r.json() == {"schema": "tenant_acme"}
```

For the storage layer, see
`adapters-storage-py/astrocyte-postgres/tests/test_postgres_tenant_isolation.py`
and `adapters-storage-py/astrocyte-age/tests/test_age_tenant_isolation.py`
for the patterns: spin up two schemas, write distinct data into each via
`use_schema()`, assert neither tenant can see the other's data.

## Where this leaves the bench

Benchmarks are **unaffected**. They use the `Astrocyte` Python API directly
(not the gateway HTTP layer), never bind a schema, and so route to
`public` exactly as before. See the bench docs for details.

## What's still pending

- **Worker rebalancing on tenant add/remove** — today, `list_tenants()` is
  snapshotted at gateway startup. Adding a new tenant requires a gateway
  restart. A periodic refresh interval is a future-work item.
- **Per-tenant rate limits and quotas** — the `TenantExtension` contract
  could grow a `get_tenant_config()` method that returns per-tenant
  overrides (recall budget, max banks, etc.). Not implemented yet.
