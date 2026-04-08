#!/usr/bin/env python3
"""Run LoCoMo or LongMemEval benchmark against Astrocyte with OpenAI.

Usage:
    python scripts/run_benchmark.py --benchmark locomo --data ./locomo10.json
    python scripts/run_benchmark.py --benchmark longmemeval --data ./LongMemEval/data
    python scripts/run_benchmark.py --benchmark locomo --data ./locomo10.json --max-questions 50

Requires OPENAI_API_KEY in the environment.
Datasets must be downloaded separately:
  - LoCoMo: https://github.com/snap-research/locomo
  - LongMemEval: https://github.com/xiaowu0162/LongMemEval
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import ClassVar

import openai

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.eval.benchmarks.locomo import LoComoBenchmark
from astrocyte.eval.benchmarks.longmemeval import LongMemEvalBenchmark
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore
from astrocyte.types import Completion, LLMCapabilities, Message, TokenUsage


# ---------------------------------------------------------------------------
# Minimal OpenAI LLM adapter (same as run_eval.py)
# ---------------------------------------------------------------------------


class OpenAIProvider:
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


async def main(benchmark: str, data_path: str, output: str | None, max_questions: int | None) -> None:
    brain = _build_brain()

    if benchmark == "locomo":
        bench = LoComoBenchmark(brain)
        result = await bench.run(
            data_path=data_path,
            bank_id="bench-locomo",
            max_questions=max_questions,
        )
        print(f"Overall accuracy:  {result.overall_accuracy:.4f}")
        print(f"Category breakdown:")
        for cat, acc in sorted(result.category_accuracy.items()):
            print(f"  {cat}: {acc:.4f}")

    elif benchmark == "longmemeval":
        bench = LongMemEvalBenchmark(brain)
        result = await bench.run(
            data_path=data_path,
            bank_id="bench-longmemeval",
            max_questions=max_questions,
        )
        print(f"Overall accuracy:  {result.overall_accuracy:.4f}")
        print(f"Category breakdown:")
        for cat, acc in sorted(result.category_accuracy.items()):
            print(f"  {cat}: {acc:.4f}")
    else:
        print(f"Unknown benchmark: {benchmark}", file=sys.stderr)
        sys.exit(1)

    # Print standard eval metrics
    m = result.eval_result.metrics
    print(f"\nRetrieval metrics:")
    print(f"  Recall precision: {m.recall_precision:.4f}")
    print(f"  Recall hit rate:  {m.recall_hit_rate:.4f}")
    print(f"  Recall MRR:       {m.recall_mrr:.4f}")
    print(f"  Retain p50/p95:   {m.retain_latency_p50_ms:.1f}ms / {m.retain_latency_p95_ms:.1f}ms")
    print(f"  Recall p50/p95:   {m.recall_latency_p50_ms:.1f}ms / {m.recall_latency_p95_ms:.1f}ms")
    print(f"  Tokens used:      {m.total_tokens_used}")
    print(f"  Duration:         {m.total_duration_seconds:.2f}s")

    if output:
        out = result.eval_result.to_dict()
        out["benchmark"] = benchmark
        out["overall_accuracy"] = result.overall_accuracy
        out["category_accuracy"] = result.category_accuracy
        with open(output, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nWrote results to {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run LoCoMo or LongMemEval benchmark")
    parser.add_argument("--benchmark", required=True, choices=["locomo", "longmemeval"],
                        help="Benchmark to run")
    parser.add_argument("--data", required=True, help="Path to dataset (file or directory)")
    parser.add_argument("--output", default=None, help="Output JSON file path")
    parser.add_argument("--max-questions", type=int, default=None,
                        help="Limit number of questions (useful for quick tests)")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY environment variable is required", file=sys.stderr)
        sys.exit(1)

    asyncio.run(main(args.benchmark, args.data, args.output, args.max_questions))
