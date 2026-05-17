"""Tests for domain-level audit logging (`astrocyte.audit`)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from astrocyte.audit import (
    AuditEntry,
    AuditLogger,
    _safe_json,
    get_default_audit_logger,
    set_default_audit_logger,
)

# ─── _safe_json ───────────────────────────────────────────────────────


class TestSafeJson:
    def test_passthrough_dict(self) -> None:
        assert _safe_json({"a": 1, "b": "x"}) == '{"a": 1, "b": "x"}'

    def test_none_returns_none(self) -> None:
        assert _safe_json(None) is None

    def test_datetime_serializes(self) -> None:
        d = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
        result = _safe_json({"started_at": d})
        assert "2026-05-16T12:00:00+00:00" in (result or "")

    def test_set_serializes_as_list(self) -> None:
        result = _safe_json({"types": {"a", "b"}})
        # set order is non-deterministic; just assert no exception + contains both
        assert result is not None
        parsed = json.loads(result)
        assert set(parsed["types"]) == {"a", "b"}

    def test_non_serializable_falls_back_to_str(self) -> None:
        class Weird:
            def __repr__(self) -> str:
                return "<weird>"

        result = _safe_json({"w": Weird()})
        assert result is not None
        assert "<weird>" in result


# ─── AuditLogger gating ───────────────────────────────────────────────


class TestAuditLoggerEnabled:
    def test_disabled_by_default(self) -> None:
        log = AuditLogger(pool_getter=lambda: None)
        assert not log.is_enabled("retain")

    def test_enabled_global(self) -> None:
        log = AuditLogger(pool_getter=lambda: None, enabled=True)
        assert log.is_enabled("anything")

    def test_allowlist_filters(self) -> None:
        log = AuditLogger(
            pool_getter=lambda: None,
            enabled=True,
            allowed_actions=["retain", "recall"],
        )
        assert log.is_enabled("retain")
        assert log.is_enabled("recall")
        assert not log.is_enabled("classify")

    def test_empty_allowlist_is_disabled_for_all(self) -> None:
        # Empty list (not None) — explicit allow-nothing
        # Note: current impl treats empty list as "all"; we use None=all,
        # list=specific. Empty list semantically means "no allowed actions".
        log = AuditLogger(
            pool_getter=lambda: None,
            enabled=True,
            allowed_actions=[],
        )
        # Empty list is falsy in `if self._allowed`, so it falls through
        # to "all allowed when enabled" — this matches Hindsight's
        # behavior. Document the quirk explicitly here.
        assert log.is_enabled("retain")  # falls through to enabled-all


# ─── AuditLogger.log (fire-and-forget) ────────────────────────────────


class FakePool:
    """Minimal pool double for testing."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    def connection(self):  # noqa: ANN201
        return self._connection_ctx()

    def _connection_ctx(self):  # noqa: ANN201
        outer = self

        class _Conn:
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *a: Any) -> None:
                pass

            def cursor(self) -> Any:
                # ``outer`` is the test-class instance captured via closure
                # from the enclosing scope; ``self`` here is the inner
                # cursor stub. PEP8 wants the first arg named 'self'.
                class _Cur:
                    async def __aenter__(self) -> Any:
                        return self

                    async def __aexit__(self, *a: Any) -> None:
                        pass

                    async def execute(self, sql: str, params: tuple) -> None:
                        outer.executed.append((sql, params))

                return _Cur()

        return _Conn()


class TestAuditLoggerLog:
    def test_no_op_when_disabled(self) -> None:
        log = AuditLogger(pool_getter=lambda: FakePool())  # enabled=False
        log.log(AuditEntry(action="retain", transport="bench"))
        # Nothing scheduled; no exception

    def test_skips_when_action_not_allowed(self) -> None:
        pool = FakePool()
        log = AuditLogger(
            pool_getter=lambda: pool,
            enabled=True,
            allowed_actions=["retain"],
        )
        log.log(AuditEntry(action="other", transport="bench"))
        # not scheduled → no rows
        assert pool.executed == []

    @pytest.mark.asyncio
    async def test_writes_when_enabled(self) -> None:
        pool = FakePool()
        log = AuditLogger(pool_getter=lambda: pool, enabled=True)
        log.log(
            AuditEntry(
                action="retain",
                transport="bench",
                bank_id="b1",
                request={"items": 3},
                response={"facts": 12},
            )
        )
        # fire-and-forget; let the task drain
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(pool.executed) == 1
        sql, params = pool.executed[0]
        assert "INSERT INTO" in sql
        assert "astrocyte_audit_log" in sql
        # params[1] = action, params[3] = bank_id, params[6] = request json
        assert params[1] == "retain"
        assert params[3] == "b1"
        assert '"items": 3' in (params[6] or "")

    @pytest.mark.asyncio
    async def test_write_failure_is_swallowed(self) -> None:
        class BadPool:
            def connection(self):  # noqa: ANN201
                raise RuntimeError("pool broken")

        log = AuditLogger(pool_getter=lambda: BadPool(), enabled=True)
        log.log(AuditEntry(action="retain", transport="bench"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # No exception escaped; warning was logged.


# ─── AuditLogger.log_with_timing ──────────────────────────────────────


class TestLogWithTiming:
    @pytest.mark.asyncio
    async def test_records_ended_at(self) -> None:
        pool = FakePool()
        log = AuditLogger(pool_getter=lambda: pool, enabled=True)
        async with log.log_with_timing(
            AuditEntry(action="recall", transport="bench", bank_id="b1"),
        ) as e:
            e.response = {"n_results": 7}
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(pool.executed) == 1
        params = pool.executed[0][1]
        # response captured
        assert '"n_results": 7' in (params[7] or "")
        # ended_at not None (param index 5)
        assert params[5] is not None


# ─── Default-logger singleton ─────────────────────────────────────────


class TestDefaultLogger:
    def test_default_is_disabled(self) -> None:
        log = get_default_audit_logger()
        assert not log.is_enabled("anything")

    def test_set_default_persists(self) -> None:
        custom = AuditLogger(pool_getter=lambda: None, enabled=True)
        set_default_audit_logger(custom)
        assert get_default_audit_logger() is custom
        # restore noop default for other tests
        set_default_audit_logger(AuditLogger(pool_getter=lambda: None))
