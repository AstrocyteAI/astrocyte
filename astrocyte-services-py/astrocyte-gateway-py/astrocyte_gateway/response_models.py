"""Documented response shapes for the v1 REST API.

These are attached to endpoints via ``responses={200: {"model": ...}}`` —
**documentation-only**: unlike FastAPI's ``response_model=`` parameter they do
NOT filter or coerce the actual payload at runtime, so declaring them cannot
change behaviour. What they do:

- put real response schemas into the OpenAPI snapshot, so the oasdiff gate
  sees response-shape changes (a removed/renamed response field is a breaking
  change, previously invisible);
- give schemathesis something to validate real responses against
  (``tests/test_openapi_conformance.py`` runs ``response_schema_conformance``).

Most endpoints return an astrocyte result dataclass serialized by
``to_jsonable`` (a faithful ``dataclasses.asdict`` with ISO datetimes), so the
dataclasses themselves are the exact contract and are referenced directly.
The Pydantic classes below cover endpoints that wrap results in ad-hoc dicts.

Deliberately not (yet) documented: the ops/introspection endpoints whose
shapes come from supervisor internals (``/health/ingest``, ``/v1/admin/sources``,
``/v1/admin/banks``, ``/v1/admin/tenants/{tenant_id}/storage``).
"""

from __future__ import annotations

from typing import Any

from astrocyte.portability import ImportResult
from astrocyte.types import (
    AuditResult,
    BankHealth,
    CompileResult,
    Entity,
    ForgetResult,
    HealthStatus,
    HistoryResult,
    LegalHold,
    LifecycleRunResult,
    MemoryHit,
    MentalModel,
    RecallResult,
    RecallTrace,
    ReflectResult,
    RetainResult,
)
from pydantic import BaseModel

__all__ = [  # noqa: RUF022 — grouped: re-exported dataclasses, then wrappers
    # astrocyte result dataclasses (exact to_jsonable shapes)
    "AuditResult",
    "BankHealth",
    "CompileResult",
    "ForgetResult",
    "HealthStatus",
    "HistoryResult",
    "ImportResult",
    "LegalHold",
    "LifecycleRunResult",
    "MentalModel",
    "RecallResult",
    "ReflectResult",
    "RetainResult",
    # wrappers for dict-shaped endpoints
    "AllBankHealthResponse",
    "DebugRecallResponse",
    "DeletedResponse",
    "DsarForgetPrincipalResponse",
    "ErrorDetail",
    "ExportResponse",
    "GraphNeighborsResponse",
    "GraphSearchResponse",
    "HoldReleasedResponse",
    "HoldStatusResponse",
    "InvalidatedResponse",
    "LiveResponse",
    "MentalModelListResponse",
]


class ErrorDetail(BaseModel):
    """Error envelope used by validation (400) and guard (401/403) responses."""

    detail: str


class LiveResponse(BaseModel):
    status: str


class DebugRecallResponse(BaseModel):
    trace: RecallTrace | None = None
    result_count: int | None = None


class GraphSearchResponse(BaseModel):
    entities: list[Entity]


class GraphNeighborsResponse(BaseModel):
    hits: list[MemoryHit]


class ExportResponse(BaseModel):
    bank_id: str
    path: str
    exported_count: int


class DsarBankDetail(BaseModel):
    bank_id: str
    deleted: int
    error: str | None = None


class DsarForgetPrincipalResponse(BaseModel):
    tenant_id: str
    principal: str
    tag_convention: str
    banks_processed: int
    memories_deleted: int
    details: list[DsarBankDetail]


class MentalModelListResponse(BaseModel):
    models: list[MentalModel]


class DeletedResponse(BaseModel):
    deleted: bool


class InvalidatedResponse(BaseModel):
    deleted: int


class HoldReleasedResponse(BaseModel):
    bank_id: str
    hold_id: str
    released: bool


class HoldStatusResponse(BaseModel):
    bank_id: str
    under_hold: bool


class AllBankHealthResponse(BaseModel):
    banks: list[BankHealth]


# Convenience: the standard error responses most endpoints can produce, for
# spreading into per-endpoint ``responses={...}`` declarations. Both carry a
# plain string ``detail`` (the 400 handler and the ConfigError→422 translation
# in app.py) — declaring 422 here also overrides FastAPI's auto-documented
# ``HTTPValidationError`` array shape, which this API never actually emits
# (request-validation failures are translated to 400).
VALIDATION_ERROR: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorDetail, "description": "Invalid request body"},
    422: {"model": ErrorDetail, "description": "Unprocessable (e.g. feature not configured)"},
}
