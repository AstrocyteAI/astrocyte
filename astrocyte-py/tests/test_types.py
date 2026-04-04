"""Tests for astrocyte.types — DTO instantiation, defaults, FFI-safety."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import get_type_hints

import pytest

from astrocyte.types import (
    AccessGrant,
    AstrocyteContext,
    AuditEvent,
    BankHealth,
    Completion,
    DataClassification,
    Dispositions,
    Document,
    EngineCapabilities,
    Entity,
    EntityLink,
    EvalMetrics,
    ForgetRequest,
    ForgetResult,
    GraphHit,
    HealthIssue,
    HealthStatus,
    HookEvent,
    MemoryHit,
    Message,
    MultiBankStrategy,
    PiiMatch,
    RecallRequest,
    RecallResult,
    ReflectRequest,
    ReflectResult,
    RetainRequest,
    RetainResult,
    TokenUsage,
    VectorHit,
    VectorItem,
)


class TestDataclassInstantiation:
    """Verify all DTOs can be created with minimal required args."""

    def test_health_status(self):
        s = HealthStatus(healthy=True)
        assert s.healthy is True
        assert s.message is None

    def test_vector_item(self):
        v = VectorItem(id="v1", bank_id="b1", vector=[0.1, 0.2], text="hello")
        assert v.id == "v1"
        assert v.tags is None

    def test_vector_hit(self):
        h = VectorHit(id="v1", text="hello", score=0.9)
        assert h.score == 0.9

    def test_entity(self):
        e = Entity(id="e1", name="Calvin", entity_type="PERSON")
        assert e.entity_type == "PERSON"
        assert e.aliases is None

    def test_entity_link(self):
        link = EntityLink(source_entity_id="e1", target_entity_id="e2", link_type="works_at")
        assert link.link_type == "works_at"

    def test_graph_hit(self):
        g = GraphHit(memory_id="m1", text="test", connected_entities=["e1"], depth=1, score=0.5)
        assert g.depth == 1

    def test_document(self):
        d = Document(id="d1", text="doc content")
        assert d.metadata is None

    def test_retain_request(self):
        r = RetainRequest(content="hello", bank_id="b1")
        assert r.content_type == "text"
        assert r.tags is None

    def test_retain_result(self):
        r = RetainResult(stored=True, memory_id="m1")
        assert r.deduplicated is False

    def test_recall_request(self):
        r = RecallRequest(query="test", bank_id="b1")
        assert r.max_results == 10
        assert r.max_tokens is None

    def test_recall_result(self):
        r = RecallResult(hits=[], total_available=0, truncated=False)
        assert r.trace is None

    def test_memory_hit(self):
        h = MemoryHit(text="hello", score=0.8)
        assert h.fact_type is None

    def test_dispositions_defaults(self):
        d = Dispositions()
        assert d.skepticism == 3
        assert d.literalism == 3
        assert d.empathy == 3

    def test_reflect_request(self):
        r = ReflectRequest(query="test", bank_id="b1")
        assert r.include_sources is True

    def test_reflect_result(self):
        r = ReflectResult(answer="synthesized")
        assert r.confidence is None

    def test_forget_request(self):
        f = ForgetRequest(bank_id="b1")
        assert f.memory_ids is None

    def test_forget_result(self):
        f = ForgetResult(deleted_count=5)
        assert f.archived_count == 0

    def test_engine_capabilities_defaults(self):
        c = EngineCapabilities()
        assert c.supports_reflect is False
        assert c.supports_semantic_search is True

    def test_engine_capabilities_frozen(self):
        c = EngineCapabilities()
        with pytest.raises(dataclasses.FrozenInstanceError):
            c.supports_reflect = True

    def test_message(self):
        m = Message(role="user", content="hello")
        assert m.role == "user"

    def test_completion(self):
        c = Completion(text="response", model="gpt-4")
        assert c.usage is None

    def test_token_usage(self):
        t = TokenUsage(input_tokens=10, output_tokens=20)
        assert t.input_tokens == 10

    def test_multi_bank_strategy_defaults(self):
        s = MultiBankStrategy()
        assert s.mode == "parallel"
        assert s.min_results_to_stop == 3
        assert s.dedup_across_banks is True

    def test_access_grant(self):
        g = AccessGrant(bank_id="b1", principal="agent:bot", permissions=["read", "write"])
        assert "write" in g.permissions

    def test_astrocyte_context(self):
        c = AstrocyteContext(principal="user:calvin")
        assert c.principal == "user:calvin"

    def test_hook_event(self):
        h = HookEvent(event_id="evt1", type="on_retain", timestamp=datetime.now(timezone.utc))
        assert h.bank_id is None

    def test_data_classification(self):
        d = DataClassification(level=3, label="restricted", categories=["PII"])
        assert d.classified_by == "rules"

    def test_audit_event(self):
        a = AuditEvent(
            event_type="memory.created",
            bank_id="b1",
            actor="user:api",
            timestamp=datetime.now(timezone.utc),
        )
        assert a.memory_ids is None

    def test_bank_health(self):
        b = BankHealth(
            bank_id="b1",
            score=0.85,
            status="healthy",
            issues=[],
            metrics={"recall_hit_rate": 0.9},
            assessed_at=datetime.now(timezone.utc),
        )
        assert b.score == 0.85

    def test_health_issue(self):
        h = HealthIssue(severity="warning", code="HIGH_DEDUP", message="High dedup rate", recommendation="Review")
        assert h.severity == "warning"

    def test_pii_match(self):
        p = PiiMatch(pii_type="email", start=0, end=15, matched_text="test@example.com")
        assert p.replacement is None

    def test_eval_metrics(self):
        m = EvalMetrics(
            recall_precision=0.8,
            recall_hit_rate=0.9,
            recall_mrr=0.7,
            recall_ndcg=0.75,
            retain_latency_p50_ms=10.0,
            retain_latency_p95_ms=50.0,
            recall_latency_p50_ms=20.0,
            recall_latency_p95_ms=100.0,
            total_tokens_used=1000,
            total_duration_seconds=30.0,
        )
        assert m.reflect_accuracy is None


class TestFFISafety:
    """Verify DTOs use only FFI-safe types."""

    # Types that are FFI-safe
    _SAFE_TYPE_NAMES = {
        "str",
        "int",
        "float",
        "bool",
        "None",
        "NoneType",
        "list",
        "dict",
        "tuple",
        "datetime",
        "date",
        "Literal",
        "ClassVar",
    }

    def test_no_any_in_dtos(self):
        """Ensure no DTO field uses typing.Any."""
        import astrocyte.types as types_module

        for name, obj in vars(types_module).items():
            if dataclasses.is_dataclass(obj) and isinstance(obj, type):
                hints = get_type_hints(obj)
                for field_name, field_type in hints.items():
                    type_str = str(field_type)
                    assert "Any" not in type_str, f"{name}.{field_name} uses Any: {type_str}"

    def test_no_callable_in_dtos(self):
        """Ensure no DTO field uses Callable."""
        import astrocyte.types as types_module

        for name, obj in vars(types_module).items():
            if dataclasses.is_dataclass(obj) and isinstance(obj, type):
                hints = get_type_hints(obj)
                for field_name, field_type in hints.items():
                    type_str = str(field_type)
                    assert "Callable" not in type_str, f"{name}.{field_name} uses Callable: {type_str}"
