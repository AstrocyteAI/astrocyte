"""Typed metadata dataclasses for Astrocyte operations.

Adopted from Hindsight (``hindsight_api/engine/operation_metadata.py``).
Provides structured shapes for the ``metadata`` field of audit entries,
async task results, and operation traces. Use these instead of
free-form ``dict[str, Any]`` so consumers can rely on stable keys.

Pairs with ``astrocyte.audit.AuditEntry.metadata`` — pass
``metadata=RecallMetadata(...).to_dict()`` to get structured audit
payloads.

The dataclasses are intentionally permissive (no nested validation,
no required-but-derivable fields). They exist to document the shape
that consumers can rely on, not to enforce a wire contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# ─── retain side ──────────────────────────────────────────────────────


@dataclass
class RetainMetadata:
    """Metadata for a single ``retain`` operation (one document/session)."""

    items_count: int  # messages or chunks ingested
    bytes_in: int = 0  # total content bytes
    facts_extracted: int = 0
    entities_extracted: int = 0
    sections_created: int = 0
    embeddings_generated: int = 0
    elapsed_ms: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BatchRetainParentMetadata:
    """Metadata for parent of split batch_retain (when payload was sub-batched)."""

    items_count: int
    total_tokens: int
    num_sub_batches: int
    is_parent: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BatchRetainChildMetadata:
    """Metadata for one sub-batch of a split batch_retain."""

    items_count: int
    parent_operation_id: str
    sub_batch_index: int
    total_sub_batches: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConsolidationMetadata:
    """Metadata for consolidation operations."""

    observations_processed: int = 0
    observations_created: int = 0
    observations_updated: int = 0
    observations_deleted: int = 0
    elapsed_ms: float = 0.0
    model: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExtractionMetadata:
    """Metadata for fact/entity extraction operations."""

    chunks_processed: int = 0
    facts_extracted: int = 0
    entities_extracted: int = 0
    elapsed_ms: float = 0.0
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── recall side ──────────────────────────────────────────────────────


@dataclass
class RecallMetadata:
    """Metadata for a single ``recall`` (search/retrieval) operation."""

    n_results: int
    top_score: float = 0.0
    strategies_used: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    cross_encoder_used: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClassifyMetadata:
    """Metadata for a question-router classification call (M16)."""

    question_type: str
    confidence: float
    effective_type: str  # may differ from question_type due to threshold
    confidence_threshold: float
    classifier_model: str
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RerankMetadata:
    """Metadata for a cross-encoder / MLX-reranker call."""

    provider: str  # "modal-cross-encoder", "mlx-jina", ...
    n_items: int
    n_returned: int
    elapsed_ms: float = 0.0
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── generic / catch-all ──────────────────────────────────────────────


@dataclass
class GenericOperationMetadata:
    """Catch-all when no typed shape applies yet."""

    operation: str
    elapsed_ms: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
