"""Tests for typed operation metadata dataclasses."""

from __future__ import annotations

from astrocyte.operation_metadata import (
    BatchRetainChildMetadata,
    BatchRetainParentMetadata,
    ClassifyMetadata,
    ConsolidationMetadata,
    ExtractionMetadata,
    GenericOperationMetadata,
    RecallMetadata,
    RerankMetadata,
    RetainMetadata,
)


class TestSerialization:
    def test_retain_metadata_to_dict(self) -> None:
        m = RetainMetadata(items_count=10, facts_extracted=42, elapsed_ms=123.4)
        d = m.to_dict()
        assert d["items_count"] == 10
        assert d["facts_extracted"] == 42
        assert d["elapsed_ms"] == 123.4
        assert "extras" in d
        assert d["extras"] == {}

    def test_recall_metadata_default_zero(self) -> None:
        m = RecallMetadata(n_results=20)
        d = m.to_dict()
        assert d["n_results"] == 20
        assert d["top_score"] == 0.0
        assert d["strategies_used"] == []
        assert not d["cross_encoder_used"]

    def test_classify_metadata_threshold(self) -> None:
        m = ClassifyMetadata(
            question_type="aggregative",
            confidence=0.72,
            effective_type="aggregative",
            confidence_threshold=0.6,
            classifier_model="gpt-4o-mini",
            elapsed_ms=89.2,
        )
        d = m.to_dict()
        assert d["question_type"] == "aggregative"
        assert d["effective_type"] == "aggregative"
        assert d["confidence_threshold"] == 0.6

    def test_classify_metadata_threshold_demotion(self) -> None:
        m = ClassifyMetadata(
            question_type="temporal",
            confidence=0.45,
            effective_type="default",
            confidence_threshold=0.6,
            classifier_model="gpt-4o-mini",
        )
        d = m.to_dict()
        # documents the demotion: classified one type, routed another
        assert d["question_type"] == "temporal"
        assert d["effective_type"] == "default"

    def test_rerank_metadata(self) -> None:
        m = RerankMetadata(provider="mlx-jina", n_items=40, n_returned=20)
        d = m.to_dict()
        assert d["provider"] == "mlx-jina"
        assert d["n_items"] == 40
        assert d["n_returned"] == 20

    def test_batch_parent_marker(self) -> None:
        m = BatchRetainParentMetadata(items_count=100, total_tokens=12000, num_sub_batches=3)
        d = m.to_dict()
        assert d["is_parent"] is True
        assert d["num_sub_batches"] == 3

    def test_batch_child_linkage(self) -> None:
        m = BatchRetainChildMetadata(
            items_count=34,
            parent_operation_id="op-abc",
            sub_batch_index=1,
            total_sub_batches=3,
        )
        d = m.to_dict()
        assert d["parent_operation_id"] == "op-abc"
        assert d["sub_batch_index"] == 1

    def test_consolidation_metadata(self) -> None:
        m = ConsolidationMetadata(
            observations_processed=50,
            observations_created=12,
            observations_updated=8,
            observations_deleted=2,
            model="gpt-4o-mini",
        )
        d = m.to_dict()
        assert d["observations_processed"] == 50
        assert d["observations_created"] + d["observations_updated"] + d["observations_deleted"] == 22

    def test_extraction_metadata(self) -> None:
        m = ExtractionMetadata(chunks_processed=5, facts_extracted=20, entities_extracted=8)
        d = m.to_dict()
        assert d["chunks_processed"] == 5

    def test_generic_fallback(self) -> None:
        m = GenericOperationMetadata(operation="custom_thing", elapsed_ms=42.0, extras={"foo": "bar"})
        d = m.to_dict()
        assert d["operation"] == "custom_thing"
        assert d["extras"]["foo"] == "bar"


class TestAuditCompatibility:
    """Confirm to_dict() output is JSON-serializable for use as AuditEntry.metadata."""

    def test_round_trip_json(self) -> None:
        import json

        m = RecallMetadata(
            n_results=18,
            top_score=0.91,
            strategies_used=["semantic", "entity"],
            elapsed_ms=150.7,
            cross_encoder_used=True,
        )
        # Should serialize cleanly with no custom encoder
        serialized = json.dumps(m.to_dict())
        loaded = json.loads(serialized)
        assert loaded["n_results"] == 18
        assert loaded["strategies_used"] == ["semantic", "entity"]
