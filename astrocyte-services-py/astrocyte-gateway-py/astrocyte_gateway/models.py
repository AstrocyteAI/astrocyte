"""Typed request models for the v1 REST API.

These models ARE the request contract: FastAPI derives the OpenAPI request
schemas from them, so the checked-in ``openapi.json`` snapshot and the oasdiff
breaking-change gate actually see field-level changes. Before this module the
handlers took ``body: dict[str, Any]`` with hand-rolled validation, which left
request bodies untyped in the schema — invisible to any contract tooling.

Error contract: validation failures return **400** with a human-readable
``detail`` string that names the offending field (clients and tests match on
the field name). FastAPI's native RequestValidationError (422) is translated in
``app.create_app`` — see ``_validation_error_to_400``.

Semantics preserved from the hand-rolled validators:

- Lax coercion: numeric strings are accepted for int fields (``"5"`` → 5).
- Unknown body keys are ignored (not rejected).
- ``max_results`` / ``limit`` / ``max_memories`` / ``max_depth`` clamp
  out-of-range values into ``[minimum, env ceiling]`` instead of rejecting —
  the DoS guard from the M1 hardening. Ceilings are read from the environment
  per request (``ASTROCYTE_MAX_RESULT_LIMIT`` / ``ASTROCYTE_GRAPH_MAX_DEPTH``).

Deliberate tightenings vs the dict-era handlers (wrong-typed OPTIONAL fields
used to be silently dropped; now they 400): ``tags``/``metadata``/``banks``
with a non-list/non-dict value, non-bool ``include_sources``/
``include_embeddings``, non-string ``set_by``/``on_conflict``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [  # noqa: RUF022 — grouped by API area, not alphabetically
    # memory core
    "RetainBody",
    "RecallBody",
    "ReflectBody",
    "ForgetBody",
    # governance / compliance
    "DsarForgetPrincipalBody",
    "ExportBody",
    "ImportBody",
    "AdminLifecycleBody",
    "AdminSetHoldBody",
    # wiki / graph / time-travel
    "CompileBody",
    "GraphSearchBody",
    "GraphNeighborsBody",
    "HistoryBody",
    "AuditBody",
    # mental models / observations
    "MentalModelCreateBody",
    "MentalModelRefreshBody",
    "ObservationsInvalidateBody",
]


def _clamp(value: int, *, minimum: int, env_var: str, default_ceiling: int) -> int:
    """Clamp into ``[minimum, ceiling]`` — ceiling read from env per request."""
    import os

    raw = os.environ.get(env_var, "").strip()
    ceiling = default_ceiling
    if raw:
        try:
            n = int(raw)
        except ValueError:
            n = 0
        if n > 0:
            ceiling = n
    if value < minimum:
        return minimum
    if value > ceiling:
        return ceiling
    return value


class _ResultLimitMixin:
    """Shared clamp for result-count fields (see _DEFAULT_RESULT_LIMIT_CEILING)."""

    @staticmethod
    def _clamp_results(v: int) -> int:
        return _clamp(v, minimum=1, env_var="ASTROCYTE_MAX_RESULT_LIMIT", default_ceiling=1000)


class RetainBody(BaseModel):
    content: str
    bank_id: str
    metadata: dict[str, Any] | None = None
    tags: list[str] | None = None


class RecallBody(BaseModel, _ResultLimitMixin):
    query: str
    bank_id: str | None = None
    banks: list[str] | None = None
    max_results: int = 10
    max_tokens: int | None = None
    tags: list[str] | None = None

    @model_validator(mode="after")
    def _require_bank_or_banks(self) -> RecallBody:
        if self.bank_id is None and self.banks is None:
            raise ValueError("bank_id or banks is required")
        return self

    @field_validator("max_results", mode="after")
    @classmethod
    def _clamp_max_results(cls, v: int) -> int:
        return cls._clamp_results(v)


class ReflectBody(BaseModel):
    query: str
    bank_id: str
    max_tokens: int | None = None
    include_sources: bool = True


class ForgetBody(BaseModel):
    bank_id: str
    memory_ids: list[str] | None = None
    tags: list[str] | None = None
    scope: Literal["all"] | None = None


class DsarForgetPrincipalBody(BaseModel):
    tenant_id: str = Field(min_length=1)
    principal: str = Field(min_length=1)


class CompileBody(BaseModel):
    bank_id: str
    scope: str | None = None


class GraphSearchBody(BaseModel, _ResultLimitMixin):
    query: str
    bank_id: str
    limit: int = 10

    @field_validator("limit", mode="after")
    @classmethod
    def _clamp_limit(cls, v: int) -> int:
        return cls._clamp_results(v)


class GraphNeighborsBody(BaseModel, _ResultLimitMixin):
    entity_ids: list[str]
    bank_id: str
    max_depth: int = 2
    limit: int = 20

    @field_validator("max_depth", mode="after")
    @classmethod
    def _clamp_depth(cls, v: int) -> int:
        return _clamp(v, minimum=0, env_var="ASTROCYTE_GRAPH_MAX_DEPTH", default_ceiling=6)

    @field_validator("limit", mode="after")
    @classmethod
    def _clamp_limit(cls, v: int) -> int:
        return cls._clamp_results(v)


class HistoryBody(BaseModel, _ResultLimitMixin):
    query: str
    bank_id: str
    as_of: datetime
    max_results: int = 10
    max_tokens: int | None = None
    tags: list[str] | None = None

    @field_validator("max_results", mode="after")
    @classmethod
    def _clamp_max_results(cls, v: int) -> int:
        return cls._clamp_results(v)


class AuditBody(BaseModel, _ResultLimitMixin):
    scope: str
    bank_id: str
    max_memories: int = 50
    max_tokens: int | None = None
    tags: list[str] | None = None

    @field_validator("max_memories", mode="after")
    @classmethod
    def _clamp_max_memories(cls, v: int) -> int:
        return cls._clamp_results(v)


class ExportBody(BaseModel):
    bank_id: str
    path: str
    include_embeddings: bool = False
    include_entities: bool = True


class ImportBody(BaseModel):
    bank_id: str
    path: str
    on_conflict: Literal["skip", "overwrite"] = "skip"


class AdminLifecycleBody(BaseModel):
    bank_id: str


class AdminSetHoldBody(BaseModel):
    hold_id: str
    reason: str
    set_by: str = "user:api"


class MentalModelCreateBody(BaseModel):
    # `model_id` is our API field, not a Pydantic-internal name.
    model_config = ConfigDict(protected_namespaces=())

    bank_id: str
    model_id: str
    title: str
    content: str
    scope: str = "bank"
    source_ids: list[str] = Field(default_factory=list)


class MentalModelRefreshBody(BaseModel):
    bank_id: str
    content: str
    source_ids: list[str] | None = None


class ObservationsInvalidateBody(BaseModel):
    bank_id: str
    source_ids: list[str]
