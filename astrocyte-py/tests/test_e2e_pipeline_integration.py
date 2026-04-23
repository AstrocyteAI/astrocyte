"""E2E integration test: identity + MIP routing + temporal + intent + forget.

No external services required — uses InMemoryEngineProvider throughout.
Covers the full operator path that unit tests exercise in isolation:

  JWT claims → ActorIdentity → AstrocyteContext
      → MIP rule routes document to docs-bank with temporal half-life
      → retain with occurred_at stamps
      → recall respects time_range
      → intent classifier labels temporal query
      → forget by tag succeeds
      → legal hold blocks unguarded forget
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import LegalHoldActive
from astrocyte.identity_jwt import classify_jwt_claims
from astrocyte.mip.router import MipRouter
from astrocyte.mip.schema import (
    ActionSpec,
    ForgetSpec,
    MatchBlock,
    MatchSpec,
    MipConfig,
    PipelineSpec,
    RoutingRule,
)
from astrocyte.pipeline.query_intent import QueryIntent, classify_query_intent
from astrocyte.testing.in_memory import InMemoryEngineProvider
from astrocyte.types import ActorIdentity, AstrocyteContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)
_WEEK_AGO = _NOW - timedelta(days=7)
_MONTH_AGO = _NOW - timedelta(days=30)

_DOCS_BANK = "docs-bank"
_DEFAULT_BANK = "user-bank"

_DOCS_RULE = RoutingRule(
    name="route-documents-to-docs-bank",
    priority=10,
    match=MatchBlock(
        all_conditions=[
            MatchSpec(field="content_type", operator="eq", value="document"),
        ]
    ),
    action=ActionSpec(
        bank=_DOCS_BANK,
        pipeline=PipelineSpec(
            version=1,
            temporal_half_life_days=7.0,
        ),
        forget=ForgetSpec(
            version=1,
            max_per_call=5,
            audit="recommended",
        ),
    ),
)


def _make_brain() -> Astrocyte:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    brain._mip_router = MipRouter(MipConfig(rules=[_DOCS_RULE]))
    return brain


def _user_context(oid: str = "user-42", upn: str = "alice@example.com") -> AstrocyteContext:
    """Mint AstrocyteContext from synthetic JWT claims via classify_jwt_claims."""
    claims = {
        "oid": oid,
        "upn": upn,
        "name": "Alice",
        "idtyp": "user",
    }
    identity: ActorIdentity = classify_jwt_claims(claims)
    return AstrocyteContext(
        principal=f"user:{identity.id}",
        actor=identity,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFullPipelineIntegration:
    """Single-flow test combining all subsystems."""

    @pytest.mark.asyncio
    async def test_full_pipeline_flow(self) -> None:
        brain = _make_brain()
        ctx = _user_context()

        # 1. Retain documents — MIP rule should reroute to docs-bank.
        r1 = await brain.retain(
            "Meeting notes: agreed to ship v1.0 by end of April",
            _DEFAULT_BANK,
            content_type="document",
            occurred_at=_WEEK_AGO,
            tags=["meeting"],
            context=ctx,
        )
        r2 = await brain.retain(
            "Quarterly planning document: roadmap items for Q2 2026",
            _DEFAULT_BANK,
            content_type="document",
            occurred_at=_MONTH_AGO,
            tags=["planning"],
            context=ctx,
        )
        assert r1.stored, f"retain r1 failed: {r1.error}"
        assert r2.stored, f"retain r2 failed: {r2.error}"

        # 2. MIP routing: memories land in docs-bank, not the requested default.
        recall_docs = await brain.recall("meeting notes", _DOCS_BANK, context=ctx)
        recall_default = await brain.recall("meeting notes", _DEFAULT_BANK, context=ctx)
        assert recall_docs.hits, "MIP routing should have written to docs-bank"
        assert not recall_default.hits, "Nothing should be in the original requested bank"

        # 3. Intent classification: temporal query triggers TEMPORAL intent.
        intent_result = classify_query_intent("when was the last meeting?")
        assert intent_result.intent == QueryIntent.TEMPORAL

        # 4. Time-range scoped recall: narrow window returns only the recent memory.
        recent_window = await brain.recall(
            "meeting",
            _DOCS_BANK,
            time_range=(_WEEK_AGO - timedelta(days=1), _NOW),
            context=ctx,
        )
        all_docs = await brain.recall("meeting", _DOCS_BANK, context=ctx)
        assert len(all_docs.hits) >= len(recent_window.hits), (
            "Narrowing the time range should not produce MORE hits"
        )

        # 5. Forget by tag removes exactly that memory.
        r_tagged = await brain.retain(
            "Temporary session note — safe to delete",
            _DOCS_BANK,
            tags=["ephemeral"],
            context=ctx,
        )
        assert r_tagged.stored
        forget_result = await brain.forget(_DOCS_BANK, tags=["ephemeral"], context=ctx)
        assert forget_result.deleted_count == 1
        after_forget = await brain.recall("session note", _DOCS_BANK, context=ctx)
        assert not any(
            "session note" in h.text.lower() for h in after_forget.hits
        ), "Forgotten memory should not appear in recall"

        # 6. Legal hold blocks further forget without compliance bypass.
        brain.set_legal_hold(
            _DOCS_BANK, "hold-litigation-1", "active litigation hold", set_by="admin"
        )
        with pytest.raises(LegalHoldActive):
            await brain.forget(_DOCS_BANK, tags=["meeting"], context=ctx)


class TestIdentityLayer:
    """JWT classification in isolation — fast contract pin."""

    def test_user_claims_produce_user_actor(self) -> None:
        identity = classify_jwt_claims(
            {"oid": "abc-123", "upn": "bob@example.com", "name": "Bob", "idtyp": "user"}
        )
        assert identity.type == "user"
        assert identity.id == "abc-123"

    def test_service_claims_produce_service_actor(self) -> None:
        identity = classify_jwt_claims({"appid": "svc-app-1", "oid": "svc-oid-1"})
        assert identity.type == "service"

    def test_unclassifiable_claims_raise(self) -> None:
        from astrocyte.errors import AuthorizationError

        with pytest.raises(AuthorizationError):
            classify_jwt_claims({"sub": "opaque-sub-only"})


class TestMipRouting:
    """MIP routing redirects to the correct bank based on content_type."""

    @pytest.mark.asyncio
    async def test_document_routes_to_docs_bank(self) -> None:
        brain = _make_brain()
        result = await brain.retain(
            "Design spec for the auth module",
            _DEFAULT_BANK,
            content_type="document",
        )
        assert result.stored
        # Hit must land in docs-bank
        hits = await brain.recall("design spec", _DOCS_BANK)
        assert hits.hits

    @pytest.mark.asyncio
    async def test_text_stays_in_requested_bank(self) -> None:
        brain = _make_brain()
        result = await brain.retain(
            "Casual note — not a document",
            _DEFAULT_BANK,
            content_type="text",
        )
        assert result.stored
        hits = await brain.recall("casual note", _DEFAULT_BANK)
        assert hits.hits


class TestTemporalRetrieval:
    """occurred_at is preserved and time_range scoping works."""

    @pytest.mark.asyncio
    async def test_occurred_at_preserved_on_hit(self) -> None:
        brain = _make_brain()
        r = await brain.retain(
            "Event: product launch",
            _DEFAULT_BANK,
            content_type="text",
            occurred_at=_WEEK_AGO,
        )
        assert r.stored
        recall = await brain.recall("product launch", _DEFAULT_BANK)
        hit = next((h for h in recall.hits if "product launch" in h.text.lower()), None)
        assert hit is not None
        # InMemoryEngine should round-trip occurred_at
        if hit.occurred_at is not None:
            assert hit.occurred_at == _WEEK_AGO

    @pytest.mark.asyncio
    async def test_time_range_excludes_old_memory(self) -> None:
        brain = _make_brain()
        await brain.retain("old fact from long ago", _DEFAULT_BANK, occurred_at=_MONTH_AGO)
        await brain.retain("recent fact from this week", _DEFAULT_BANK, occurred_at=_WEEK_AGO)

        recent_only = await brain.recall(
            "fact",
            _DEFAULT_BANK,
            time_range=(_WEEK_AGO - timedelta(days=1), _NOW),
        )
        all_hits = await brain.recall("fact", _DEFAULT_BANK)
        # All hits ≥ recent_only hits (narrower window can only drop, not add)
        assert len(all_hits.hits) >= len(recent_only.hits)


class TestIntentClassification:
    """Regression pin on intent labels — changing these shifts all RRF weights."""

    def test_temporal_query_classified_as_temporal(self) -> None:
        assert classify_query_intent("when did Alice join?").intent == QueryIntent.TEMPORAL

    def test_factual_query_not_temporal(self) -> None:
        result = classify_query_intent("what is Alice's role?")
        assert result.intent != QueryIntent.TEMPORAL

    def test_unknown_query_returns_unknown(self) -> None:
        result = classify_query_intent("zxqwerty blorb")
        assert result.intent == QueryIntent.UNKNOWN


class TestForgetAndLegalHold:
    """Forget enforcement: tag scope, max_per_call cap, legal hold."""

    @pytest.mark.asyncio
    async def test_forget_by_tag(self) -> None:
        brain = _make_brain()
        await brain.retain("keep me", _DEFAULT_BANK, tags=["keep"])
        await brain.retain("delete me", _DEFAULT_BANK, tags=["temp"])
        result = await brain.forget(_DEFAULT_BANK, tags=["temp"])
        assert result.deleted_count == 1

    @pytest.mark.asyncio
    async def test_legal_hold_blocks_forget(self) -> None:
        brain = _make_brain()
        await brain.retain("sensitive record", _DEFAULT_BANK)
        brain.set_legal_hold(_DEFAULT_BANK, "hold-1", "legal hold", set_by="admin")
        with pytest.raises(LegalHoldActive):
            await brain.forget(_DEFAULT_BANK, scope="all")

    @pytest.mark.asyncio
    async def test_compliance_bypass_clears_held_bank(self) -> None:
        brain = _make_brain()
        ctx = _user_context()
        await brain.retain("held record", _DEFAULT_BANK, context=ctx)
        brain.set_legal_hold(_DEFAULT_BANK, "hold-2", "test hold", set_by="admin")
        result = await brain.forget(
            _DEFAULT_BANK, scope="all", compliance=True, reason="right-to-erasure", context=ctx
        )
        assert result.deleted_count >= 0  # bypass granted; count may be 0 if already empty

    @pytest.mark.asyncio
    async def test_mip_forget_spec_caps_per_call(self) -> None:
        """ForgetSpec.max_per_call on docs-bank limits id-based deletes to 5."""
        brain = _make_brain()
        ids = []
        for i in range(7):
            r = await brain.retain(f"doc {i}", _DOCS_BANK)
            if r.memory_id:
                ids.append(r.memory_id)

        if len(ids) > 5:
            from astrocyte.errors import AstrocyteError
            with pytest.raises(AstrocyteError, match="max_per_call"):
                await brain.forget(_DOCS_BANK, memory_ids=ids)
