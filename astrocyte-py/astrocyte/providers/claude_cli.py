"""Claude Code CLI LLMProvider adapter — completion via the local ``claude`` binary.

Runs ``claude -p`` (print mode) as a subprocess for every ``complete()`` call.
Authentication is the CLI's own logged-in subscription: no ANTHROPIC_API_KEY is
required, and any key present in the environment is deliberately NOT passed to
the subprocess, so calls bill the subscription rather than an API key.

Usage programmatically (the bench harness path)::

    from astrocyte.providers.claude_cli import ClaudeCliProvider

    llm = ClaudeCliProvider(model="haiku")

Limitations (inherent to the CLI surface, documented rather than hidden):

- ``temperature`` and ``max_tokens`` are accepted for protocol compatibility
  but IGNORED — the CLI exposes no sampling controls. Results are therefore a
  separate ablation track, never comparable to API-provider runs.
- ``tools`` / native function calling is NOT supported and raises
  ``NotImplementedError`` (loud failure beats silently dropping tool calls).
- ``response_format`` (json_object / json_schema) is EMULATED by appending a
  JSON-only instruction to the prompt. Downstream Astrocyte stages already
  parse with ``_json_tolerant``, so prose slippage degrades gracefully.
- ``embed()`` is NOT supported — the CLI has no embeddings surface. Compose
  with :class:`~astrocyte.providers.local_embeddings.LocalEmbeddingsProvider`
  via :class:`~astrocyte.providers.composite.CompositeLLMProvider`.
- Each call pays ~1-3s of process startup on top of inference. Concurrency is
  bounded by a semaphore (``ASTROCYTE_CLAUDE_CLI_MAX_CONCURRENCY``, default 4).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from typing import ClassVar

from astrocyte.types import (
    Completion,
    ContentPart,
    LLMCapabilities,
    Message,
    ToolDefinition,
)

logger = logging.getLogger("astrocyte.providers.claude_cli")


def _flatten_content(content: str | list[ContentPart]) -> str:
    """Render Message.content to plain text. Non-text parts are dropped
    with a log line — the CLI is a text surface."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        text = getattr(part, "text", None)
        if text:
            parts.append(text)
        else:
            logger.debug("claude_cli: dropping non-text content part %r", type(part))
    return "\n".join(parts)


def _render_prompt(messages: list[Message], response_format: dict | None) -> str:
    """Flatten a chat transcript into a single print-mode prompt.

    System messages float to the top (in order); user/assistant turns are
    role-labelled only when there is more than one non-system message, so
    the common single-user-message case stays a clean bare prompt.
    """
    system_parts = [_flatten_content(m.content) for m in messages if m.role == "system"]
    convo = [m for m in messages if m.role != "system"]

    if len(convo) <= 1:
        body = _flatten_content(convo[0].content) if convo else ""
    else:
        body = "\n\n".join(
            f"[{m.role}]\n{_flatten_content(m.content)}" for m in convo
        )

    blocks: list[str] = []
    if system_parts:
        blocks.append("\n\n".join(p for p in system_parts if p))
    if body:
        blocks.append(body)

    if response_format is not None:
        rf_type = response_format.get("type") if isinstance(response_format, dict) else None
        if rf_type == "json_schema":
            schema = response_format.get("json_schema", {})
            blocks.append(
                "IMPORTANT: Respond with a single valid JSON object conforming "
                "to this JSON schema. No prose, no code fences.\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            )
        else:  # json_object and any future variants degrade to the generic rule
            blocks.append(
                "IMPORTANT: Respond with a single valid JSON object only. "
                "No prose, no code fences."
            )

    return "\n\n---\n\n".join(blocks)


def _normalize_json_output(text: str) -> str | None:
    """Best-effort normalization of model output to a bare JSON value.

    Emulated JSON mode (see module docstring) means the model can wrap the
    object in ```json fences or surrounding prose. Returns the canonical
    JSON string when a parseable value can be recovered, else ``None``.

    Recovery ladder (first hit wins):
    1. The text parses as-is.
    2. Code-fence stripped (```json ... ``` or bare ``` fences).
    3. Outermost ``{...}`` or ``[...]`` slice of the raw text.
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        json.loads(stripped)
        return stripped
    except json.JSONDecodeError:
        pass

    if stripped.startswith("```"):
        newline = stripped.find("\n")
        inner = stripped[newline + 1 :] if newline != -1 else stripped[3:]
        inner = inner.rstrip()
        if inner.endswith("```"):
            inner = inner[:-3].rstrip()
        try:
            json.loads(inner)
            return inner
        except json.JSONDecodeError:
            stripped = inner  # fall through to brace slicing on the unfenced body

    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = stripped.find(open_ch)
        end = stripped.rfind(close_ch)
        if start != -1 and end > start:
            candidate = stripped[start : end + 1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue
    return None


class ClaudeCliProvider:
    """LLMProvider whose ``complete()`` shells out to the Claude Code CLI."""

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        *,
        model: str = "haiku",
        binary: str | None = None,
        timeout: float = 180.0,
        max_retries: int = 3,
        max_concurrency: int | None = None,
    ) -> None:
        resolved = binary or os.environ.get("CLAUDE_CLI_BIN") or shutil.which("claude")
        if not resolved:
            raise RuntimeError(
                "ClaudeCliProvider requires the Claude Code CLI on PATH "
                "(or CLAUDE_CLI_BIN=/path/to/claude)."
            )
        self._bin = resolved
        self._model = model
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        conc = max_concurrency or int(
            os.environ.get("ASTROCYTE_CLAUDE_CLI_MAX_CONCURRENCY", "4")
        )
        self._sem = asyncio.Semaphore(max(1, conc))
        # Private empty cwd: print-mode runs must not pick up any project's
        # CLAUDE.md context.
        self._cwd = tempfile.mkdtemp(prefix="astrocyte-claude-cli-")
        self._warned_model_override = False

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            supports_multimodal_completion=False,
            modalities_supported=("text",),
            supports_multimodal_embedding=False,
            supports_batch_embed=False,
        )

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,  # noqa: ARG002 — CLI limitation, see module docstring
        temperature: float = 0.0,  # noqa: ARG002 — CLI limitation
        tools: list[ToolDefinition] | None = None,
        tool_choice: str | None = None,  # noqa: ARG002
        response_format: dict | None = None,
    ) -> Completion:
        if tools:
            raise NotImplementedError(
                "ClaudeCliProvider does not support native function calling "
                "(tools=...). Use an API-backed provider for agentic paths."
            )
        # Per-call model overrides carry OpenAI names from legacy call sites
        # (e.g. summarizer_model='gpt-4o-mini'); the CLI model wins. Warn once.
        if model and model != self._model and not self._warned_model_override:
            logger.warning(
                "claude_cli: ignoring per-call model override %r — using %r "
                "(further overrides silenced)", model, self._model,
            )
            self._warned_model_override = True

        prompt = _render_prompt(messages, response_format)
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        last_error: str = ""
        for attempt in range(self._max_retries):
            try:
                async with self._sem:
                    proc = await asyncio.create_subprocess_exec(
                        self._bin,
                        "-p",
                        "--model", self._model,
                        "--output-format", "text",
                        "--max-turns", "1",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=self._cwd,
                        env=env,
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            proc.communicate(prompt.encode("utf-8")),
                            timeout=self._timeout,
                        )
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                        raise
                if proc.returncode == 0:
                    text = stdout.decode("utf-8", "replace").strip()
                    if response_format is not None:
                        # Emulated JSON mode: normalize fences/prose to bare
                        # JSON so downstream ``json.loads`` behaves as it
                        # would under native constrained decoding. An
                        # unparseable response counts as a failed attempt
                        # and is retried; after the final attempt we fall
                        # back to the RAW text (never raise) — pipeline
                        # stages have their own tolerant parsing and
                        # graceful-degradation paths.
                        normalized = _normalize_json_output(text)
                        if normalized is not None:
                            text = normalized
                        elif attempt < self._max_retries - 1:
                            last_error = "unparseable JSON-mode output"
                            logger.warning(
                                "claude_cli: JSON-mode output unparseable "
                                "(attempt %d/%d) — retrying",
                                attempt + 1, self._max_retries,
                            )
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                        else:
                            logger.warning(
                                "claude_cli: JSON-mode output unparseable "
                                "after %d attempts — returning raw text",
                                self._max_retries,
                            )
                    return Completion(
                        text=text,
                        model=self._model,
                        usage=None,  # CLI print mode reports no token usage
                        tool_calls=None,
                    )
                last_error = stderr.decode("utf-8", "replace").strip()[:500]
                logger.warning(
                    "claude_cli complete attempt %d/%d exited %s: %s",
                    attempt + 1, self._max_retries, proc.returncode, last_error,
                )
            except asyncio.TimeoutError:
                last_error = f"timeout after {self._timeout}s"
                logger.warning(
                    "claude_cli complete attempt %d/%d timed out",
                    attempt + 1, self._max_retries,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)[:500]
                logger.warning(
                    "claude_cli complete attempt %d/%d failed: %s",
                    attempt + 1, self._max_retries, exc,
                )
            if attempt < self._max_retries - 1:
                await asyncio.sleep(2 * (attempt + 1))

        raise RuntimeError(f"claude_cli complete failed after {self._max_retries} attempts: {last_error}")

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        raise NotImplementedError(
            "The Claude Code CLI has no embeddings surface. Compose "
            "ClaudeCliProvider with LocalEmbeddingsProvider via "
            "CompositeLLMProvider (astrocyte.providers.composite)."
        )
