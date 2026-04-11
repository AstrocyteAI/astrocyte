"""Smolagents (HuggingFace) integration — Astrocyte as @tool functions.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.smolagents import astrocyte_smolagent_tools

    brain = Astrocyte.from_config("astrocyte.yaml")
    tools = astrocyte_smolagent_tools(brain, bank_id="user-123")

    # Use with smolagents
    from smolagents import CodeAgent, HfApiModel
    agent = CodeAgent(tools=tools, model=HfApiModel())

Smolagents uses a code-centric approach where tools are plain Python functions
with type annotations and docstrings. The agent writes Python code that calls
these functions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte

from astrocyte.types import AstrocyteContext


class AstrocyteSmolTool:
    """A single Astrocyte tool compatible with smolagents' Tool protocol.

    Smolagents expects tools with: name, description, inputs (schema), output_type,
    and a __call__ or forward method.
    """

    def __init__(
        self,
        name: str,
        description: str,
        inputs: dict[str, dict[str, str]],
        output_type: str,
        fn: Any,
    ) -> None:
        self.name = name
        self.description = description
        self.inputs = inputs
        self.output_type = output_type
        self._fn = fn

    async def forward(self, **kwargs: Any) -> Any:
        """Execute the tool (async)."""
        return await self._fn(**kwargs)

    def __call__(self, **kwargs: Any) -> Any:
        """Sync fallback — smolagents may call tools synchronously."""
        import asyncio

        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, self._fn(**kwargs)).result()
        return asyncio.run(self._fn(**kwargs))


def astrocyte_smolagent_tools(
    brain: Astrocyte,
    bank_id: str,
    *,
    context: AstrocyteContext | None = None,
    include_reflect: bool = True,
    include_forget: bool = False,
) -> list[AstrocyteSmolTool]:
    """Create smolagents-compatible tools backed by Astrocyte.

    Returns a list of AstrocyteSmolTool instances that implement the
    smolagents Tool protocol (name, description, inputs, output_type, forward).
    """
    tools: list[AstrocyteSmolTool] = []

    async def _retain(content: str, tags: str = "") -> str:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        result = await brain.retain(content, bank_id=bank_id, tags=tag_list, context=context)
        if result.stored:
            return f"Stored memory with id: {result.memory_id}"
        return f"Failed to store: {result.error}"

    tools.append(
        AstrocyteSmolTool(
            name="memory_retain",
            description="Store content into long-term memory for future recall.",
            inputs={
                "content": {"type": "string", "description": "The text to memorize."},
                "tags": {"type": "string", "description": "Comma-separated tags (optional)."},
            },
            output_type="string",
            fn=_retain,
        )
    )

    async def _recall(query: str, max_results: int = 5) -> str:
        result = await brain.recall(query, bank_id=bank_id, max_results=max_results, context=context)
        if not result.hits:
            return "No relevant memories found."
        lines = [f"- [{h.score:.2f}] {h.text}" for h in result.hits]
        return f"Found {len(result.hits)} memories:\n" + "\n".join(lines)

    tools.append(
        AstrocyteSmolTool(
            name="memory_recall",
            description="Search long-term memory for information relevant to a query.",
            inputs={
                "query": {"type": "string", "description": "Natural language search query."},
                "max_results": {"type": "integer", "description": "Maximum number of results."},
            },
            output_type="string",
            fn=_recall,
        )
    )

    if include_reflect:

        async def _reflect(query: str) -> str:
            result = await brain.reflect(query, bank_id=bank_id, context=context)
            return result.answer

        tools.append(
            AstrocyteSmolTool(
                name="memory_reflect",
                description="Synthesize a comprehensive answer from long-term memory.",
                inputs={
                    "query": {"type": "string", "description": "The question to answer."},
                },
                output_type="string",
                fn=_reflect,
            )
        )

    if include_forget:

        async def _forget(memory_ids: str) -> str:
            ids = [mid.strip() for mid in memory_ids.split(",")]
            result = await brain.forget(bank_id, memory_ids=ids, context=context)
            return f"Deleted {result.deleted_count} memories."

        tools.append(
            AstrocyteSmolTool(
                name="memory_forget",
                description="Remove specific memories by their IDs (comma-separated).",
                inputs={
                    "memory_ids": {"type": "string", "description": "Comma-separated memory IDs."},
                },
                output_type="string",
                fn=_forget,
            )
        )

    return tools
