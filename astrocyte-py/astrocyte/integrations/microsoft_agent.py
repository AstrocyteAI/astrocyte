"""Microsoft Agent Framework integration.

Usage:
    from astrocyte import Astrocyte
    from astrocyte.integrations.microsoft_agent import astrocyte_ms_agent_tools

    brain = Astrocyte.from_config("astrocyte.yaml")
    tools, handlers = astrocyte_ms_agent_tools(brain, bank_id="user-123")

    # Register with Microsoft Agent Framework
    agent = Agent(tools=tools)

Microsoft Agent Framework uses OpenAI-compatible tool definitions with
handler functions, similar to the OpenAI Agents SDK pattern. This integration
provides the same format with Astrocytes-specific tool names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


def astrocyte_ms_agent_tools(
    brain: Astrocyte,
    bank_id: str,
    *,
    include_reflect: bool = True,
    include_forget: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Create Microsoft Agent Framework-compatible tools.

    Returns (tools, handlers) — same pattern as openai_agents integration
    since Microsoft Agent Framework uses OpenAI-compatible tool definitions.
    """
    from astrocyte.integrations.openai_agents import astrocyte_tool_definitions

    return astrocyte_tool_definitions(
        brain,
        bank_id,
        include_reflect=include_reflect,
        include_forget=include_forget,
    )
