#!/usr/bin/env python3
"""Run an Astrocyte eval suite with OpenAI as the LLM backend.

Usage:
    python scripts/run_eval.py                  # defaults to "basic" suite
    python scripts/run_eval.py --suite accuracy
    python scripts/run_eval.py --suite basic --output eval-result.json

Requires OPENAI_API_KEY in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import ClassVar

import openai

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.eval import MemoryEvaluator
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore
from astrocyte.types import Completion, LLMCapabilities, Message, TokenUsage


# ---------------------------------------------------------------------------
# Minimal OpenAI LLM adapter (inline — not a full provider package)
# ---------------------------------------------------------------------------


class OpenAIProvider:
    """Minimal LLMProvider backed by the OpenAI SDK.

    Supports complete() and embed() — enough for the eval pipeline.
    """

    SPI_VERSION: ClassVar[int] = 1

    def __init__(
        self,
        api_key: str | None = None,
        completion_model: str = "gpt-4o",
        embedding_model: str = "text-embedding-3-small",
    ) -> None:
        self._client = openai.AsyncOpenAI(api_key=api_key)
        self._completion_model = completion_model
        self._embedding_model = embedding_model

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities()

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Completion:
        resp = await self._client.chat.completions.create(
            model=model or self._completion_model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        choice = resp.choices[0]
        usage = None
        if resp.usage:
            usage = TokenUsage(
                input_tokens=resp.usage.prompt_tokens,
                output_tokens=resp.usage.completion_tokens,
            )
        return Completion(
            text=choice.message.content or "",
            model=resp.model,
            usage=usage,
        )

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        resp = await self._client.embeddings.create(
            model=model or self._embedding_model,
            input=texts,
        )
        return [item.embedding for item in resp.data]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _build_brain() -> Astrocyte:
    config = AstrocyteConfig()
    config.provider_tier = "storage"
    config.barriers.pii.mode = "disabled"
    config.escalation.degraded_mode = "error"

    brain = Astrocyte(config)
    vector_store = InMemoryVectorStore()
    llm = OpenAIProvider()
    pipeline = PipelineOrchestrator(vector_store=vector_store, llm_provider=llm)
    brain.set_pipeline(pipeline)
    return brain


async def main(suite: str, output: str | None) -> None:
    brain = _build_brain()
    evaluator = MemoryEvaluator(brain)
    result = await evaluator.run_suite(suite, bank_id="eval-ci")

    # Write JSON artifact
    if output:
        with open(output, "w") as f:
            f.write(result.to_json())

    # Print summary
    m = result.metrics
    print(f"Suite:            {result.suite}")
    print(f"Provider:         {result.provider} (Tier: {result.provider_tier})")
    print(f"Recall precision: {m.recall_precision:.4f}")
    print(f"Recall hit rate:  {m.recall_hit_rate:.4f}")
    print(f"Recall MRR:       {m.recall_mrr:.4f}")
    print(f"Recall NDCG:      {m.recall_ndcg:.4f}")
    print(f"Retain p50/p95:   {m.retain_latency_p50_ms:.1f}ms / {m.retain_latency_p95_ms:.1f}ms")
    print(f"Recall p50/p95:   {m.recall_latency_p50_ms:.1f}ms / {m.recall_latency_p95_ms:.1f}ms")
    print(f"Tokens used:      {m.total_tokens_used}")
    print(f"Duration:         {m.total_duration_seconds:.2f}s")
    if m.reflect_accuracy is not None:
        print(f"Reflect accuracy: {m.reflect_accuracy:.4f}")


if __name__ == "__main__":
    if not os.environ.get("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY environment variable is required", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Run Astrocyte eval suite")
    parser.add_argument("--suite", default="basic", help="Suite name (basic, accuracy) or path to YAML suite")
    parser.add_argument("--output", default=None, help="Output JSON file path")
    args = parser.parse_args()

    asyncio.run(main(args.suite, args.output))
