"""AML (Agent Memory Leaderboard) adapter — Add/Search HTTP service.

Exposes Astrocyte as a participating memory system for the Agent Memory
Leaderboard (https://agentmemoryleaderboard.ai). AML is the field's only
multi-institution matched-harness evaluation: participants expose ONLY
``Add`` and ``Search``; the platform owns answer generation, judging,
scoring, and orchestration.

Design rules this service is bound by
-------------------------------------

1. **Public API only.** Every call goes through ``Astrocyte.retain()`` /
   ``Astrocyte.recall()``. No harness-private shortcuts — the M32 parity
   lesson (bench measuring a code path users never run) applies with
   force here, because AML publishes the result.

2. **Search returns memories, never answers.** AML's integrity rule:
   "Search must not generate final answers or disguise answers as memory
   records." ``recall()`` is synthesis-free by construction (it returns
   retained/derived memory with provenance; synthesis lives in
   ``reflect()``, which this service never calls). See
   ``docs/_design/sota-roadmap-m45-m48.md`` §9.2.

3. **Add confirms searchability before returning 200.** The contract:
   HTTP 200 "only after persistence and searchability confirmed".
   ``retain()`` completes extraction + embedding + storage synchronously;
   we additionally surface ``stored``/``error`` as a hard failure so the
   platform's retry logic sees a non-200 rather than a false success.

4. **user_id is the isolation boundary.** Mapped 1:1 to ``bank_id``.
   AML forbids sharing memories across user_ids; Astrocyte's per-bank
   access boundary enforces this at the storage layer. ``session_id`` is
   grouping metadata only and is explicitly NOT used as a search filter
   (per contract), so M31's ``session_filter`` stays unset.

Contract reference: https://agentmemoryleaderboard.ai/api-guide
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from astrocyte import log_safe as _safe_log
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("astrocyte.aml")

# AML formal evaluations request top_k=100. Our banked evidence (M30) is
# that answerer accuracy peaks near 50 candidates and degrades past it
# through context dilution — and under AML the platform's answer model
# consumes exactly what we return. top_k is a MAXIMUM, not a quota, so we
# return our best N rather than padding to 100. Override per-cycle via env
# while calibrating against the leaderboard.
DEFAULT_RESULT_CAP = int(os.environ.get("ASTROCYTE_AML_RESULT_CAP", "50"))

# Recall breadth requested from the pipeline before the cap is applied.
RECALL_FETCH_K = int(os.environ.get("ASTROCYTE_AML_FETCH_K", "100"))


# ── Wire models (mirror the AML contract exactly) ────────────────────────


class AddMessage(BaseModel):
    role: str
    content: str
    timestamp: int | None = None  # Unix milliseconds


class AddRequest(BaseModel):
    request_id: str
    messages: list[AddMessage]
    user_id: str
    session_id: str


class AddResponse(BaseModel):
    success: bool
    request_id: str
    user_id: str
    session_id: str


class SearchRequest(BaseModel):
    query: str
    user_id: str
    top_k: int = 100
    options: list[str] | None = None  # choice questions only


class SearchItem(BaseModel):
    id: str
    content: str
    score: float | None = None
    created_at: str | None = None


class SearchResponse(BaseModel):
    data: list[SearchItem] = Field(default_factory=list)


# ── Rendering ────────────────────────────────────────────────────────────


def _ms_to_dt(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def render_conversation(messages: list[AddMessage]) -> str:
    """Render an AML message batch into Conversation-Engine input.

    Uses the ``**{role}**: {content}`` convention the Conversation Engine
    already parses (M17), with an inline ISO timestamp per turn when the
    platform supplies one — retain-time temporal anchoring is what lets
    the pipeline resolve relative dates without query-time date math
    (M31 Fix 4).
    """
    lines: list[str] = []
    for m in messages:
        ts = _ms_to_dt(m.timestamp)
        stamp = f" [{ts.isoformat()}]" if ts else ""
        lines.append(f"**{m.role}**{stamp}: {m.content}")
    return "\n\n".join(lines)


def render_hit_content(hit: Any) -> str:
    """Render one recall hit as a self-contained evidence string.

    The platform's answer model sees ONLY this string — it has no access
    to our metadata, scores, or schema. So temporal and speaker context
    must be inlined or it is lost. This is the core-side counterpart of
    the M28-B bench renderer, whose inline ``occurred``/``mentioned``
    pairing produced measurable answerer gains (roadmap §9.1).
    """
    parts: list[str] = []

    occurred = getattr(hit, "occurred_at", None)
    retained = getattr(hit, "retained_at", None)
    when: list[str] = []
    if occurred is not None:
        when.append(f"occurred {occurred.date().isoformat()}")
    if retained is not None and (occurred is None or retained.date() != occurred.date()):
        when.append(f"recorded {retained.date().isoformat()}")
    if when:
        parts.append(f"[{'; '.join(when)}]")

    meta = getattr(hit, "metadata", None) or {}
    speaker = meta.get("speaker") if isinstance(meta, dict) else None
    if speaker:
        parts.append(f"({speaker})")

    parts.append(getattr(hit, "text", "") or "")
    return " ".join(p for p in parts if p).strip()


# ── App ──────────────────────────────────────────────────────────────────


def create_app(brain: Any | None = None) -> FastAPI:
    """Build the AML adapter app.

    Args:
        brain: An ``Astrocyte`` instance. When None, one is constructed at
            startup from ``ASTROCYTE_CONFIG_PATH`` (or library defaults),
            using the same wiring path as any other deployment.
    """
    app = FastAPI(title="Astrocyte AML adapter", version="1")
    app.state.brain = brain

    async def _brain() -> Any:
        if app.state.brain is None:
            from astrocyte import Astrocyte

            cfg = os.environ.get("ASTROCYTE_CONFIG_PATH")
            app.state.brain = (
                Astrocyte.from_config(cfg) if cfg else Astrocyte.from_config_dict({})
            )
        return app.state.brain

    def _check_auth(request: Request) -> None:
        """Optional shared-secret gate (AML supports Token/Bearer/X-Api-Key).

        Unset ``ASTROCYTE_AML_API_KEY`` means public (smoke-test) mode.
        """
        expected = os.environ.get("ASTROCYTE_AML_API_KEY")
        if not expected:
            return
        supplied = request.headers.get("x-api-key") or ""
        auth = request.headers.get("authorization", "")
        for prefix in ("Bearer ", "Token "):
            if auth.startswith(prefix):
                supplied = supplied or auth[len(prefix):]
        if supplied != expected:
            # 401 is in AML's do-not-retry set — correct for a real auth failure.
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/add", response_model=AddResponse)
    async def add(req: AddRequest, request: Request) -> AddResponse:
        _check_auth(request)
        if not req.messages:
            # Contract error — AML does not retry 400/422.
            raise HTTPException(status_code=400, detail="messages must be non-empty")

        brain = await _brain()
        content = render_conversation(req.messages)

        # Earliest turn timestamp anchors the batch in domain time.
        stamps = [t for t in (_ms_to_dt(m.timestamp) for m in req.messages) if t]
        occurred_at = min(stamps) if stamps else None

        try:
            result = await brain.retain(
                content,
                bank_id=req.user_id,           # user_id IS the isolation boundary
                content_type="conversation",   # Conversation Engine path (M17)
                occurred_at=occurred_at,
                source="aml",
                metadata={
                    "session_id": req.session_id,
                    "aml_request_id": req.request_id,
                },
            )
        except Exception as exc:
            logger.exception("aml.add failed request_id=%s", _safe_log(req.request_id))
            # 500 is retried by the platform with bounded backoff.
            raise HTTPException(status_code=500, detail=f"retain failed: {exc}") from exc

        # Only report success once the content is persisted AND searchable.
        # Surfacing a pipeline-level failure as 500 lets AML retry rather
        # than silently scoring us on memories we never stored. Dedup is a
        # legitimate no-op store — the content is already searchable.
        not_stored = getattr(result, "error", None) or not getattr(result, "stored", False)
        if not_stored and not getattr(result, "deduplicated", False):
            detail = getattr(result, "error", None) or "retain did not store content"
            raise HTTPException(status_code=500, detail=detail)

        return AddResponse(
            success=True,
            request_id=req.request_id,
            user_id=req.user_id,
            session_id=req.session_id,
        )

    @app.post("/search", response_model=SearchResponse)
    async def search(req: SearchRequest, request: Request) -> SearchResponse:
        _check_auth(request)
        brain = await _brain()

        # Choice questions: the option text is signal for retrieval, but the
        # query must stay the platform's question for answer fidelity — so
        # options are appended only to the retrieval query.
        retrieval_query = req.query
        if req.options:
            retrieval_query = f"{req.query}\n" + "\n".join(req.options)

        try:
            result = await brain.recall(
                retrieval_query,
                bank_id=req.user_id,
                max_results=max(RECALL_FETCH_K, req.top_k),
                # session_id deliberately unset: AML specifies session_id is
                # for grouping only and must not filter Search.
            )
        except Exception as exc:
            logger.exception("aml.search failed user_id=%s", _safe_log(req.user_id))
            raise HTTPException(status_code=500, detail=f"recall failed: {exc}") from exc

        cap = min(req.top_k, DEFAULT_RESULT_CAP)
        items: list[SearchItem] = []
        for i, hit in enumerate(getattr(result, "hits", [])[:cap]):
            content = render_hit_content(hit)
            if not content:
                continue  # contract: content must be a non-empty string
            created = getattr(hit, "occurred_at", None) or getattr(hit, "retained_at", None)
            items.append(
                SearchItem(
                    id=getattr(hit, "memory_id", None) or f"mem_{i}",
                    content=content,
                    score=getattr(hit, "score", None),
                    created_at=created.isoformat() if created else None,
                )
            )

        return SearchResponse(data=items)

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    return app


app = create_app()
