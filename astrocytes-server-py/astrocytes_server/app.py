"""FastAPI application exposing Astrocytes over REST."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from astrocytes.errors import (
    AccessDenied,
    CapabilityNotSupported,
    ConfigError,
    PiiRejected,
    ProviderUnavailable,
    RateLimited,
)
from astrocytes.types import AstrocyteContext

from astrocytes_server.brain import build_astrocyte
from astrocytes_server.serialization import to_jsonable


def create_app() -> FastAPI:
    brain = build_astrocyte()

    app = FastAPI(
        title="Astrocytes reference server",
        description="REST API over astrocytes-py (Tier 1 in-memory pipeline by default).",
        version="0.1.0",
    )

    @app.exception_handler(AccessDenied)
    async def _access_denied(_request: Request, exc: AccessDenied) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(RateLimited)
    async def _rate_limited(_request: Request, exc: RateLimited) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc), "retry_after_seconds": exc.retry_after_seconds},
        )

    @app.exception_handler(ConfigError)
    async def _config_error(_request: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(CapabilityNotSupported)
    async def _capability(_request: Request, exc: CapabilityNotSupported) -> JSONResponse:
        return JSONResponse(
            status_code=501,
            content={"detail": str(exc), "provider": exc.provider, "capability": exc.capability},
        )

    @app.exception_handler(PiiRejected)
    async def _pii(_request: Request, exc: PiiRejected) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc), "pii_types": exc.pii_types})

    @app.exception_handler(ProviderUnavailable)
    async def _provider_unavailable(_request: Request, exc: ProviderUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "provider": exc.provider},
        )

    def context_from_principal(principal: str | None) -> AstrocyteContext | None:
        if principal is None or principal == "":
            return None
        return AstrocyteContext(principal=principal)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        status = await brain.health()
        return to_jsonable(status)

    @app.post("/v1/retain")
    async def retain(
        body: dict[str, Any],
        x_astrocytes_principal: Annotated[str | None, Header(alias="X-Astrocytes-Principal")] = None,
    ) -> dict[str, Any]:
        content = body.get("content")
        bank_id = body.get("bank_id")
        if not isinstance(content, str) or not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="content and bank_id (str) are required")
        metadata = body.get("metadata")
        tags = body.get("tags")
        ctx = context_from_principal(x_astrocytes_principal)
        result = await brain.retain(
            content,
            bank_id,
            metadata=metadata if isinstance(metadata, dict) else None,
            tags=tags if isinstance(tags, list) else None,
            context=ctx,
        )
        return to_jsonable(result)

    @app.post("/v1/recall")
    async def recall(
        body: dict[str, Any],
        x_astrocytes_principal: Annotated[str | None, Header(alias="X-Astrocytes-Principal")] = None,
    ) -> dict[str, Any]:
        query = body.get("query")
        bank_id = body.get("bank_id")
        banks = body.get("banks")
        if not isinstance(query, str):
            raise HTTPException(status_code=400, detail="query (str) is required")
        max_results = int(body["max_results"]) if body.get("max_results") is not None else 10
        max_tokens = body.get("max_tokens")
        if max_tokens is not None:
            max_tokens = int(max_tokens)
        tags = body.get("tags")
        if bank_id is not None and not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="bank_id must be a string")
        if banks is not None and not isinstance(banks, list):
            raise HTTPException(status_code=400, detail="banks must be a list of strings")
        ctx = context_from_principal(x_astrocytes_principal)
        result = await brain.recall(
            query,
            bank_id=bank_id if isinstance(bank_id, str) else None,
            banks=[str(x) for x in banks] if isinstance(banks, list) else None,
            max_results=max_results,
            max_tokens=max_tokens,
            tags=[str(x) for x in tags] if isinstance(tags, list) else None,
            context=ctx,
        )
        return to_jsonable(result)

    @app.post("/v1/reflect")
    async def reflect(
        body: dict[str, Any],
        x_astrocytes_principal: Annotated[str | None, Header(alias="X-Astrocytes-Principal")] = None,
    ) -> dict[str, Any]:
        query = body.get("query")
        bank_id = body.get("bank_id")
        if not isinstance(query, str) or not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="query and bank_id (str) are required")
        max_tokens = body.get("max_tokens")
        if max_tokens is not None:
            max_tokens = int(max_tokens)
        include_sources = body.get("include_sources", True)
        ctx = context_from_principal(x_astrocytes_principal)
        result = await brain.reflect(
            query,
            bank_id,
            max_tokens=max_tokens,
            include_sources=bool(include_sources),
            context=ctx,
        )
        return to_jsonable(result)

    @app.post("/v1/forget")
    async def forget(
        body: dict[str, Any],
        x_astrocytes_principal: Annotated[str | None, Header(alias="X-Astrocytes-Principal")] = None,
    ) -> dict[str, Any]:
        bank_id = body.get("bank_id")
        if not isinstance(bank_id, str):
            raise HTTPException(status_code=400, detail="bank_id (str) is required")
        memory_ids = body.get("memory_ids")
        tags = body.get("tags")
        ctx = context_from_principal(x_astrocytes_principal)
        result = await brain.forget(
            bank_id,
            memory_ids=[str(x) for x in memory_ids] if isinstance(memory_ids, list) else None,
            tags=[str(x) for x in tags] if isinstance(tags, list) else None,
            context=ctx,
        )
        return to_jsonable(result)

    return app
