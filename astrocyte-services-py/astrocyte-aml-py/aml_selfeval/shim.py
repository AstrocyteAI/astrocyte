"""OpenAI-shaped shim so AML's pipelines can drive *any* Astrocyte provider.

AML's published pipelines reach their answerer and judge through one
hard-coded call shape::

    POST {ANSWER_API_BASE}/chat/completions
    {"model": ..., "messages": [{"role": "user", "content": ...}],
     "temperature": 0}
    -> response["choices"][0]["message"]["content"]

That is an OpenAI *wire format*, not an OpenAI *dependency*. Astrocyte is
LLM-agnostic by design — providers resolve through the entry-point SPI
(``astrocyte.llm_providers``) — so requiring an OpenAI account to
self-evaluate would be a defect in this harness, not a property of AML.

This module closes that gap: it serves the one endpoint the pipelines
call and fulfils it with whatever provider the SPI resolves. Point
``ANSWER_API_BASE``/``JUDGE_API_BASE`` at this shim and AML's prompts and
judge rubric run **unmodified** against the Claude CLI, a local model, or
anything else registered under the SPI.

Configuration mirrors the ``astrocyte.yaml`` provider keys, so the same
name that is valid in config is valid here::

    ASTROCYTE_SELFEVAL_PROVIDER          # entry-point name or "module:Class"
    ASTROCYTE_SELFEVAL_PROVIDER_CONFIG   # JSON kwargs for its constructor

Fidelity caveats, stated rather than hidden:

* ``temperature`` is forwarded, but providers that cannot honour it (the
  Claude CLI has no temperature flag) will ignore it. Judge labels stay
  stable in practice because the rubric is constrained, but this is not
  bit-for-bit determinism.
* The wire ``model`` is passed through verbatim, so ``ANSWER_MODEL`` means
  whatever the configured provider says it means. Leave it empty to use
  the provider's own default.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("astrocyte.aml.selfeval.shim")

DEFAULT_PROVIDER = os.environ.get("ASTROCYTE_SELFEVAL_PROVIDER", "claude_cli")
DEFAULT_MAX_TOKENS = int(os.environ.get("ASTROCYTE_SELFEVAL_MAX_TOKENS", "1024"))


# ── Wire models (the OpenAI subset the pipelines actually use) ───────────


class ChatMessage(BaseModel):
    role: str
    content: str = ""


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: str | None = None
    temperature: float = 0.0
    max_tokens: int | None = None


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]
    usage: dict[str, int] = Field(default_factory=dict)


# ── Provider construction (SPI, never a hard-coded class) ────────────────


def build_provider(name: str | None = None, config: dict[str, Any] | None = None) -> Any:
    """Resolve and construct an LLM provider through the Astrocyte SPI.

    ``name`` accepts either an entry-point name registered under
    ``astrocyte.llm_providers`` (e.g. ``claude_cli``) or a direct
    ``module:Class`` path — exactly what ``llm_provider:`` accepts in
    ``astrocyte.yaml``. No provider is special-cased here.
    """
    from astrocyte._discovery import resolve_provider

    resolved_name = name or DEFAULT_PROVIDER
    if config is None:
        raw = os.environ.get("ASTROCYTE_SELFEVAL_PROVIDER_CONFIG", "")
        config = json.loads(raw) if raw.strip() else {}

    cls = resolve_provider(resolved_name, "llm_providers")
    logger.info("selfeval shim provider=%s class=%s", resolved_name, cls.__name__)
    return cls(**config)


# ── App ──────────────────────────────────────────────────────────────────


def create_app(provider: Any | None = None) -> FastAPI:
    """Build the shim app.

    Args:
        provider: A constructed LLM provider. When None, one is built at
            first use from the ``ASTROCYTE_SELFEVAL_PROVIDER`` env vars —
            deferred so importing this module never shells out or loads
            model weights.
    """
    app = FastAPI(title="Astrocyte AML self-eval shim", version="1")
    app.state.provider = provider

    def _provider() -> Any:
        if app.state.provider is None:
            app.state.provider = build_provider()
        return app.state.provider

    def _check_auth(request: Request) -> None:
        """Optional gate. Unset means open — this is a localhost harness."""
        expected = os.environ.get("ASTROCYTE_SELFEVAL_API_KEY")
        if not expected:
            return
        auth = request.headers.get("authorization", "")
        supplied = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if supplied != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

    async def _complete(req: ChatRequest, request: Request) -> ChatResponse:
        _check_auth(request)
        if not req.messages:
            raise HTTPException(status_code=400, detail="messages must be non-empty")

        from astrocyte.types import Message

        provider = _provider()
        messages = [Message(role=m.role, content=m.content) for m in req.messages]

        try:
            completion = await provider.complete(
                messages,
                # Empty/absent model means "provider default" rather than
                # forcing a name the provider may not recognise.
                model=req.model or None,
                max_tokens=req.max_tokens or DEFAULT_MAX_TOKENS,
                temperature=req.temperature,
            )
        except Exception as exc:
            logger.exception("selfeval shim completion failed")
            # 500 so the caller's own retry/backoff sees a transient failure.
            raise HTTPException(status_code=500, detail=f"completion failed: {exc}") from exc

        usage_obj = getattr(completion, "usage", None)
        usage: dict[str, int] = {}
        if usage_obj is not None:
            prompt = getattr(usage_obj, "input_tokens", 0)
            output = getattr(usage_obj, "output_tokens", 0)
            usage = {
                "prompt_tokens": prompt,
                "completion_tokens": output,
                "total_tokens": prompt + output,
            }

        return ChatResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
            created=int(time.time()),
            model=getattr(completion, "model", None) or req.model or "",
            choices=[
                ChatChoice(
                    message=ChatMessage(
                        role="assistant",
                        content=getattr(completion, "text", "") or "",
                    )
                )
            ],
            usage=usage,
        )

    # AML sets ANSWER_API_BASE and appends "/chat/completions". Mount both
    # with and without the /v1 prefix so either base URL spelling works.
    app.post("/v1/chat/completions", response_model=ChatResponse)(_complete)
    app.post("/chat/completions", response_model=ChatResponse)(_complete)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "provider": DEFAULT_PROVIDER}

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"message": exc.detail, "type": "shim_error"}},
        )

    return app


app = create_app()
