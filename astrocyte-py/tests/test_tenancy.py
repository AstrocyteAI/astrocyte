"""Tests for ``astrocyte.tenancy`` — schema-per-tenant primitives.

This module is the SQL-injection guard for the schema-per-tenant feature.
Every adapter SQL string ultimately routes through :func:`fq_table`, so the
identifier validation here MUST be airtight.
"""

from __future__ import annotations

import asyncio

import pytest

from astrocyte.tenancy import (
    DEFAULT_SCHEMA,
    AuthenticationError,
    DefaultTenantExtension,
    Tenant,
    TenantContext,
    TenantExtension,
    fq_function,
    fq_table,
    get_current_schema,
    reset_current_schema,
    set_current_schema,
    use_schema,
)


class TestDefaultBehavior:
    """Without any tenant binding, everything resolves to the default schema."""

    def test_default_schema_is_public(self):
        # Reset any leftover state from prior tests in the same process.
        from astrocyte.tenancy import _current_schema

        _current_schema.set(None)
        assert get_current_schema() == "public"
        assert DEFAULT_SCHEMA == "public"

    def test_fq_table_uses_default_when_unset(self):
        from astrocyte.tenancy import _current_schema

        _current_schema.set(None)
        assert fq_table("astrocyte_vectors") == '"public"."astrocyte_vectors"'

    def test_fq_function_uses_default_when_unset(self):
        from astrocyte.tenancy import _current_schema

        _current_schema.set(None)
        assert fq_function("astrocyte_vectors_fts_update") == '"public"."astrocyte_vectors_fts_update"'


class TestExplicitSchemaArgument:
    """Workers and migration runners pass the schema explicitly."""

    def test_fq_table_with_explicit_schema_overrides_context(self):
        with use_schema("tenant_acme"):
            assert fq_table("memory_units", schema="tenant_globex") == '"tenant_globex"."memory_units"'

    def test_fq_table_with_explicit_schema_works_outside_context(self):
        assert fq_table("memory_units", schema="tenant_acme") == '"tenant_acme"."memory_units"'


class TestUseSchemaContextManager:
    """``use_schema`` is the preferred binding API — never forgets to reset."""

    def test_binds_schema_for_block(self):
        with use_schema("tenant_acme"):
            assert get_current_schema() == "tenant_acme"
            assert fq_table("astrocyte_vectors") == '"tenant_acme"."astrocyte_vectors"'

    def test_restores_previous_schema_on_exit(self):
        from astrocyte.tenancy import _current_schema

        _current_schema.set(None)
        assert get_current_schema() == "public"
        with use_schema("tenant_acme"):
            assert get_current_schema() == "tenant_acme"
        assert get_current_schema() == "public"

    def test_restores_on_exception(self):
        from astrocyte.tenancy import _current_schema

        _current_schema.set(None)
        # Plain try/except (instead of pytest.raises wrapping the with-block)
        # so CodeQL's reachability analysis sees the post-block assert as
        # reachable — pytest.raises hides the exception flow from static
        # analyzers, triggering a false-positive "unreachable code" warning.
        raised = False
        try:
            with use_schema("tenant_acme"):
                raise RuntimeError("boom")
        except RuntimeError as exc:
            assert "boom" in str(exc)
            raised = True
        assert raised
        assert get_current_schema() == "public"

    def test_nested_blocks_stack_correctly(self):
        from astrocyte.tenancy import _current_schema

        _current_schema.set(None)
        with use_schema("tenant_acme"):
            with use_schema("tenant_globex"):
                assert get_current_schema() == "tenant_globex"
            assert get_current_schema() == "tenant_acme"
        assert get_current_schema() == "public"


class TestSetResetTokenApi:
    """Lower-level API for middleware that can't use a context manager."""

    def test_set_returns_token_that_resets(self):
        from astrocyte.tenancy import _current_schema

        _current_schema.set(None)
        token = set_current_schema("tenant_acme")
        try:
            assert get_current_schema() == "tenant_acme"
        finally:
            reset_current_schema(token)
        assert get_current_schema() == "public"


class TestContextVarAsyncIsolation:
    """ContextVar must propagate per-task — concurrent tenants stay isolated."""

    @pytest.mark.asyncio
    async def test_concurrent_tasks_see_independent_schemas(self):
        results: dict[str, str] = {}

        async def worker(name: str, schema: str) -> None:
            with use_schema(schema):
                # Yield to let other tasks interleave; the schema should
                # remain bound for THIS task only.
                await asyncio.sleep(0.01)
                results[name] = get_current_schema()

        await asyncio.gather(
            worker("a", "tenant_acme"),
            worker("b", "tenant_globex"),
            worker("c", "tenant_initech"),
        )

        assert results == {
            "a": "tenant_acme",
            "b": "tenant_globex",
            "c": "tenant_initech",
        }


class TestIdentifierValidation:
    """The SQL-injection guard. Every malformed identifier MUST raise."""

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            " ",
            "1leading_digit",
            "with-hyphen",
            "with space",
            "with.dot",
            'with"quote',
            "with;semicolon",
            "with'apostrophe",
            "drop table users",
            "schema; DROP TABLE x; --",
            "tenant\x00null",
            "🚀rocket",
        ],
    )
    def test_fq_table_rejects_malformed_table_names(self, bad: str):
        with pytest.raises(ValueError, match="not a valid PostgreSQL identifier"):
            fq_table(bad)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "1starts_with_digit",
            "has-hyphen",
            "has space",
            "has.dot",
            'has"quote',
            "has;semicolon",
            "DROP TABLE x",
            "tenant_acme; DELETE FROM x; --",
        ],
    )
    def test_fq_table_rejects_malformed_schema_names(self, bad: str):
        with pytest.raises(ValueError, match="not a valid PostgreSQL identifier"):
            fq_table("astrocyte_vectors", schema=bad)

    @pytest.mark.parametrize(
        "bad",
        ["", "starts with space", "1digit", "has-hyphen", "has;injection"],
    )
    def test_set_current_schema_rejects_malformed(self, bad: str):
        with pytest.raises(ValueError, match="not a valid PostgreSQL identifier"):
            set_current_schema(bad)

    def test_use_schema_rejects_malformed(self):
        with pytest.raises(ValueError, match="not a valid PostgreSQL identifier"):
            with use_schema("not safe"):
                pass

    def test_default_extension_rejects_malformed_schema(self):
        with pytest.raises(ValueError, match="not a valid PostgreSQL identifier"):
            DefaultTenantExtension(schema="bad-name")

    @pytest.mark.parametrize(
        "good",
        ["public", "tenant_acme", "TenantGlobex", "schema_with_underscore", "_leading_underscore", "x", "S1"],
    )
    def test_accepts_valid_identifiers(self, good: str):
        # should not raise
        result = fq_table("astrocyte_vectors", schema=good)
        assert result == f'"{good}"."astrocyte_vectors"'


class TestNonStringTypeRejection:
    """Non-string inputs (None, int, bytes) MUST be rejected without crashing."""

    @pytest.mark.parametrize("bad", [None, 42, 3.14, b"bytes", [], {}, object()])
    def test_fq_table_rejects_non_string_table(self, bad):
        with pytest.raises(ValueError):
            fq_table(bad)  # type: ignore[arg-type]


class TestDefaultTenantExtension:
    """The shipped single-tenant implementation."""

    @pytest.mark.asyncio
    async def test_authenticate_returns_configured_schema(self):
        ext = DefaultTenantExtension(schema="tenant_acme")
        ctx = await ext.authenticate(context=None)
        assert isinstance(ctx, TenantContext)
        assert ctx.schema_name == "tenant_acme"

    @pytest.mark.asyncio
    async def test_authenticate_default_is_public(self):
        ext = DefaultTenantExtension()
        ctx = await ext.authenticate(context=None)
        assert ctx.schema_name == "public"

    @pytest.mark.asyncio
    async def test_list_tenants_returns_single_entry(self):
        ext = DefaultTenantExtension(schema="tenant_acme")
        tenants = await ext.list_tenants()
        assert tenants == [Tenant(schema="tenant_acme")]

    @pytest.mark.asyncio
    async def test_authenticate_ignores_request_payload(self):
        # The default extension must not throw on any request shape.
        ext = DefaultTenantExtension()
        for payload in [None, {}, {"api_key": "anything"}, "string", 42]:
            ctx = await ext.authenticate(context=payload)
            assert ctx.schema_name == "public"


class TestCustomTenantExtension:
    """Verify the contract works for a realistic API-key-based extension."""

    @pytest.mark.asyncio
    async def test_custom_extension_can_map_key_to_schema(self):
        class ApiKeyExtension(TenantExtension):
            def __init__(self, key_to_schema: dict[str, str]) -> None:
                self._map = key_to_schema

            async def authenticate(self, context):
                key = context.get("api_key") if isinstance(context, dict) else None
                if key not in self._map:
                    raise AuthenticationError(f"unknown key: {key}")
                return TenantContext(schema_name=self._map[key])

            async def list_tenants(self):
                return [Tenant(schema=s) for s in sorted(set(self._map.values()))]

        ext = ApiKeyExtension({"key-acme": "tenant_acme", "key-globex": "tenant_globex"})

        acme = await ext.authenticate({"api_key": "key-acme"})
        assert acme.schema_name == "tenant_acme"

        globex = await ext.authenticate({"api_key": "key-globex"})
        assert globex.schema_name == "tenant_globex"

        with pytest.raises(AuthenticationError, match="unknown key"):
            await ext.authenticate({"api_key": "nope"})

        tenants = await ext.list_tenants()
        assert sorted(t.schema for t in tenants) == ["tenant_acme", "tenant_globex"]
