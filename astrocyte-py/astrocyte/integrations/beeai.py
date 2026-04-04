"""BeeAI / IBM Bee Agent Framework integration.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.beeai import astrocyte_bee_tools

    brain = Astrocyte.from_config("astrocyte.yaml")
    tools = astrocyte_bee_tools(brain, bank_id="user-123")

    # Register with BeeAI agent
    agent = BeeAgent(llm=llm, tools=tools)

BeeAI uses a tool pattern where each tool has a name, description,
input schema, and an async handler function. Similar to OpenAI tools
but with BeeAI-specific tool class wrappers.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


class AstrocyteBeeTool:
    """A single Astrocyte tool compatible with BeeAI's Tool interface.

    BeeAI expects tools with: name, description, input_schema, and a run() method.
    """

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Any,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self._handler = handler

    async def run(self, input_data: dict[str, Any]) -> str:
        """Execute the tool. Returns a string result."""
        return await self._handler(input_data)


def astrocyte_bee_tools(
    brain: Astrocyte,
    bank_id: str,
    *,
    include_reflect: bool = True,
    include_forget: bool = False,
) -> list[AstrocyteBeeTool]:
    """Create BeeAI-compatible tools backed by Astrocyte."""
    tools: list[AstrocyteBeeTool] = []

    async def _retain(input_data: dict[str, Any]) -> str:
        content = input_data["content"]
        tags = input_data.get("tags")
        tag_list = [t.strip() for t in tags.split(",")] if isinstance(tags, str) and tags else tags
        result = await brain.retain(content, bank_id=bank_id, tags=tag_list)
        return json.dumps({"stored": result.stored, "memory_id": result.memory_id})

    tools.append(
        AstrocyteBeeTool(
            name="memory_retain",
            description="Store content into long-term memory.",
            input_schema={"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
            handler=_retain,
        )
    )

    async def _recall(input_data: dict[str, Any]) -> str:
        query = input_data["query"]
        max_results = input_data.get("max_results", 5)
        result = await brain.recall(query, bank_id=bank_id, max_results=max_results)
        hits = [{"text": h.text, "score": round(h.score, 4)} for h in result.hits]
        return json.dumps({"hits": hits, "total": result.total_available})

    tools.append(
        AstrocyteBeeTool(
            name="memory_recall",
            description="Search long-term memory for relevant information.",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            handler=_recall,
        )
    )

    if include_reflect:

        async def _reflect(input_data: dict[str, Any]) -> str:
            result = await brain.reflect(input_data["query"], bank_id=bank_id)
            return result.answer

        tools.append(
            AstrocyteBeeTool(
                name="memory_reflect",
                description="Synthesize an answer from long-term memory.",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                handler=_reflect,
            )
        )

    if include_forget:

        async def _forget(input_data: dict[str, Any]) -> str:
            ids = input_data["memory_ids"]
            if isinstance(ids, str):
                ids = [mid.strip() for mid in ids.split(",")]
            result = await brain.forget(bank_id, memory_ids=ids)
            return json.dumps({"deleted_count": result.deleted_count})

        tools.append(
            AstrocyteBeeTool(
                name="memory_forget",
                description="Remove specific memories by their IDs.",
                input_schema={
                    "type": "object",
                    "properties": {"memory_ids": {"type": "array", "items": {"type": "string"}}},
                    "required": ["memory_ids"],
                },
                handler=_forget,
            )
        )

    return tools
