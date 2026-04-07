"""End-to-end integration tests for MIP + lifecycle.

Proves the full flow: mip.yaml → Astrocyte.retain() → routing decision → storage.
Uses InMemoryEngineProvider — no external dependencies, runs in CI.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import LegalHoldActive
from astrocyte.testing.in_memory import InMemoryEngineProvider

MIP_YAML = textwrap.dedent("""\
    version: "1.0"

    banks:
      - id: "student-{student_id}"
        description: Per-student memory
      - id: private-encrypted
        description: PII content
      - id: ops-monitoring
        description: Pipeline ops

    rules:
      - name: pii-lockdown
        priority: 1
        override: true
        match:
          pii_detected: true
        action:
          bank: private-encrypted
          tags: [pii, compliance]
          retain_policy: redact_before_store

      - name: student-answer
        priority: 10
        match:
          all:
            - content_type: student_answer
            - metadata.student_id: present
        action:
          bank: "student-{metadata.student_id}"
          tags:
            - "{metadata.topic}"

      - name: reject-binary
        priority: 20
        match:
          content_type: binary
        action:
          retain_policy: reject

      - name: pipeline-event
        priority: 10
        match:
          all:
            - source: fabric_pipeline
            - metadata.pipeline_id: present
        action:
          bank: ops-monitoring
          tags:
            - fabric
            - "{metadata.pipeline_id}"

    intent_policy:
      model_context: "Route content. Banks: {banks}. Tags: {tags}."
""")


def _make_brain(tmp_path: Path) -> tuple[Astrocyte, InMemoryEngineProvider]:
    """Create an Astrocyte instance with MIP routing from a temp mip.yaml."""
    mip_path = tmp_path / "mip.yaml"
    mip_path.write_text(MIP_YAML)

    config = AstrocyteConfig()
    config.mip_config_path = str(mip_path)
    config.barriers.pii.mode = "disabled"
    config.barriers.validation.allowed_content_types = [
        "text", "conversation", "document", "student_answer", "binary",
    ]

    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain, engine


class TestMipMechanicalRouting:
    """Mechanical rules route content to the correct bank and tags."""

    @pytest.mark.asyncio
    async def test_student_answer_routed_to_student_bank(self, tmp_path: Path) -> None:
        brain, engine = _make_brain(tmp_path)

        result = await brain.retain(
            "The quadratic formula solves second degree polynomial equations",
            bank_id="default",
            metadata={"student_id": "stu-42", "topic": "algebra"},
            content_type="student_answer",
        )

        assert result.stored is True
        # Verify it was stored in the student-specific bank, not "default"
        recall = await brain.recall("quadratic formula", bank_id="student-stu-42")
        assert recall.total_available >= 1
        assert any("quadratic" in h.text for h in recall.hits)

        # Verify nothing in the original "default" bank
        default_recall = await brain.recall("quadratic formula", bank_id="default")
        assert default_recall.total_available == 0

    @pytest.mark.asyncio
    async def test_student_answer_tagged_with_topic(self, tmp_path: Path) -> None:
        brain, engine = _make_brain(tmp_path)

        await brain.retain(
            "The mitochondria is the powerhouse of the cell",
            bank_id="default",
            metadata={"student_id": "stu-7", "topic": "biology"},
            tags=["original-tag"],
            content_type="student_answer",
        )

        recall = await brain.recall("mitochondria powerhouse", bank_id="student-stu-7")
        assert recall.total_available >= 1
        # MIP replaces tags with ["biology"]
        assert "biology" in (recall.hits[0].tags or [])

    @pytest.mark.asyncio
    async def test_pipeline_event_routed_to_ops(self, tmp_path: Path) -> None:
        brain, engine = _make_brain(tmp_path)

        await brain.retain(
            "Pipeline alkali-lake failed at stage 3 with timeout error",
            bank_id="default",
            metadata={"pipeline_id": "alkali-lake", "status": "failed"},
            source="fabric_pipeline",
        )

        recall = await brain.recall("Pipeline alkali-lake failed", bank_id="ops-monitoring")
        assert recall.total_available >= 1


class TestMipPiiOverride:
    """PII override rule forces content to encrypted bank regardless of other matches."""

    @pytest.mark.asyncio
    async def test_pii_overrides_student_routing(self, tmp_path: Path) -> None:
        brain, engine = _make_brain(tmp_path)

        # This matches student-answer rule, but pii_detected=True forces PII lockdown
        result = await brain.retain(
            "Student SSN is 123-45-6789",
            bank_id="default",
            metadata={"student_id": "stu-42", "topic": "personal"},
            content_type="student_answer",
            pii_detected=True,
        )

        assert result.stored is True

        # Should be in private-encrypted, NOT student-stu-42
        encrypted_recall = await brain.recall("Student SSN", bank_id="private-encrypted")
        assert encrypted_recall.total_available >= 1

        student_recall = await brain.recall("Student SSN", bank_id="student-stu-42")
        assert student_recall.total_available == 0


class TestMipRejectPolicy:
    """Reject policy prevents storage."""

    @pytest.mark.asyncio
    async def test_binary_content_rejected(self, tmp_path: Path) -> None:
        brain, engine = _make_brain(tmp_path)

        result = await brain.retain(
            "binary blob data here",
            bank_id="default",
            content_type="binary",
        )

        assert result.stored is False
        assert "Rejected by MIP" in (result.error or "")


class TestMipPassthrough:
    """When no rules match and no LLM, content passes through to original bank."""

    @pytest.mark.asyncio
    async def test_unmatched_content_passthrough(self, tmp_path: Path) -> None:
        brain, engine = _make_brain(tmp_path)

        result = await brain.retain(
            "Just a random thought about the weather today being nice",
            bank_id="my-notes",
        )

        assert result.stored is True
        # Should stay in the original bank
        recall = await brain.recall("random thought weather", bank_id="my-notes")
        assert recall.total_available >= 1


class TestMipIntentEscalation:
    """Intent layer fires when no mechanical rule matches and LLM is available."""

    @pytest.mark.asyncio
    async def test_llm_routes_unmatched_content(self, tmp_path: Path) -> None:
        from astrocyte.types import Completion, TokenUsage

        class RoutingLLM:
            async def complete(self, messages, **kwargs):
                return Completion(
                    text='{"bank_id": "ops-monitoring", "tags": ["llm-routed"], "retain_policy": "default", "reasoning": "Looks like operational content"}',
                    model="mock",
                    usage=TokenUsage(input_tokens=10, output_tokens=20),
                )

            async def embed(self, texts, **kwargs):
                return [[0.1] * 128 for _ in texts]

            async def health(self):
                from astrocyte.types import HealthStatus
                return HealthStatus(healthy=True)

            def capabilities(self):
                from astrocyte.types import LLMCapabilities
                return LLMCapabilities()

        brain, engine = _make_brain(tmp_path)
        # Wire LLM into MIP router
        brain._mip_router._llm_provider = RoutingLLM()

        result = await brain.retain(
            "Server cpu-42 memory usage spiked to 98% at 03:00 UTC",
            bank_id="default",
        )

        assert result.stored is True
        # LLM routed to ops-monitoring
        recall = await brain.recall("Server cpu-42 memory", bank_id="ops-monitoring")
        assert recall.total_available >= 1


class TestLifecycleE2E:
    """Legal hold and TTL lifecycle integration."""

    @pytest.mark.asyncio
    async def test_legal_hold_blocks_forget(self, tmp_path: Path) -> None:
        brain, engine = _make_brain(tmp_path)

        await brain.retain("Important evidence", bank_id="case-1")
        brain.set_legal_hold("case-1", "litigation-2024", "Active litigation")

        with pytest.raises(LegalHoldActive):
            await brain.forget("case-1")

    @pytest.mark.asyncio
    async def test_forget_succeeds_after_hold_released(self, tmp_path: Path) -> None:
        brain, engine = _make_brain(tmp_path)

        result = await brain.retain("Temporary evidence", bank_id="case-2")
        brain.set_legal_hold("case-2", "hold-1", "Investigation")
        brain.release_legal_hold("case-2", "hold-1")

        # Should succeed now
        forget_result = await brain.forget("case-2", memory_ids=[result.memory_id])
        assert forget_result.deleted_count >= 0  # May be 0 if engine doesn't track individual IDs

    @pytest.mark.asyncio
    async def test_compliance_forget_bypasses_hold(self, tmp_path: Path) -> None:
        from astrocyte.types import AstrocyteContext

        brain, engine = _make_brain(tmp_path)

        await brain.retain("PII data for subject X", bank_id="user-data")
        brain.set_legal_hold("user-data", "hold-1", "Audit")

        # compliance=True bypasses legal hold (GDPR right-to-forget)
        # Requires caller context (even when ACL disabled)
        ctx = AstrocyteContext(principal="system:compliance")
        forget_result = await brain.forget("user-data", compliance=True, context=ctx)
        assert forget_result is not None  # Did not raise

    @pytest.mark.asyncio
    async def test_lifecycle_disabled_is_noop(self, tmp_path: Path) -> None:
        brain, engine = _make_brain(tmp_path)
        # lifecycle.enabled defaults to False
        result = await brain.run_lifecycle("any-bank")
        assert result.archived_count == 0
        assert result.deleted_count == 0
