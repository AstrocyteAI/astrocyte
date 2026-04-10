"""Claude Managed Agents integration — session-aware memory for cloud-hosted agents.

Managed Agents (https://claude.com/blog/claude-managed-agents) handles
orchestration, sandboxing, and tool execution. Astrocyte handles what the
agent remembers, for how long, and under what compliance rules.

This module provides helpers for the two integration patterns:

1. **Single-agent with session-scoped memory**:
   Memory banks are tied to SDK session IDs so each session gets isolated memory.

2. **Multi-agent coordination**:
   A coordinator agent shares a bank namespace with sub-agents.
   Each sub-agent writes to its own bank; all agents can read from
   the shared coordinator bank. MIP routes cross-agent writes.

Usage — single agent:
    from astrocyte import Astrocyte
    from astrocyte.integrations.managed_agents import create_memory_server

    brain = Astrocyte.from_config("astrocyte.yaml")
    server = create_memory_server(brain, session_id="sess-abc123")

    options = ClaudeAgentOptions(
        mcp_servers={"memory": server},
        allowed_tools=["mcp__memory__*"],
    )

Usage — multi-agent:
    from astrocyte.integrations.managed_agents import (
        create_coordinator_server,
        create_subagent_definition,
    )

    coord_server = create_coordinator_server(brain, session_id="sess-abc")
    researcher = create_subagent_definition(
        brain, role="researcher", session_id="sess-abc",
        description="Research agent that explores files and web sources.",
        prompt="You are a research specialist...",
        tools=["Read", "Grep", "Glob", "WebSearch", "WebFetch"],
    )

    options = ClaudeAgentOptions(
        mcp_servers={"memory": coord_server},
        allowed_tools=["mcp__memory__*", "Agent"],
        agents={"researcher": researcher},
    )
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrocyte._astrocyte import Astrocyte


# ---------------------------------------------------------------------------
# Bank naming conventions
# ---------------------------------------------------------------------------

def session_bank_id(session_id: str) -> str:
    """Derive a bank_id from an SDK session ID."""
    return f"session-{session_id}"


def agent_bank_id(session_id: str, role: str) -> str:
    """Derive a per-agent bank_id within a session."""
    return f"session-{session_id}:agent-{role}"


def coordinator_bank_id(session_id: str) -> str:
    """The shared coordinator bank for a multi-agent session."""
    return f"session-{session_id}:coordinator"


# ---------------------------------------------------------------------------
# Shared helpers — parse tags
# ---------------------------------------------------------------------------

def _parse_tags(raw: Any) -> list[str] | None:
    """Parse comma-separated tag string into a list."""
    if isinstance(raw, str) and raw:
        return [t.strip() for t in raw.split(",")]
    return None


def _format_sdk_response(data: Any) -> dict[str, Any]:
    """Wrap data in SDK MCP response format."""
    text = data if isinstance(data, str) else json.dumps(data)
    return {"content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# Handler logic — testable without SDK dependency
# ---------------------------------------------------------------------------

async def _handle_retain(
    brain: Astrocyte,
    bank_id: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Retain handler: store content into a specific bank."""
    tag_list = _parse_tags(args.get("tags"))
    result = await brain.retain(args["content"], bank_id=bank_id, tags=tag_list)
    return _format_sdk_response({"stored": result.stored, "memory_id": result.memory_id})


async def _handle_coordinator_recall(
    brain: Astrocyte,
    session_id: str,
    coord_bank: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Coordinator recall: search coordinator bank + optional sub-agent banks."""
    query_text = args["query"]
    max_results = args.get("max_results", 10)
    include_agents = args.get("include_agents", "")

    banks = [coord_bank]
    if include_agents:
        for role in include_agents.split(","):
            role = role.strip()
            if role:
                banks.append(agent_bank_id(session_id, role))

    result = await brain.recall(
        query_text, banks=banks, strategy="parallel", max_results=max_results
    )
    hits = [
        {"text": h.text, "score": round(h.score, 4), "bank_id": h.bank_id}
        for h in result.hits
    ]
    return _format_sdk_response({"hits": hits, "total": result.total_available})


async def _handle_coordinator_reflect(
    brain: Astrocyte,
    session_id: str,
    coord_bank: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Coordinator reflect: synthesize from coordinator + optional sub-agent banks."""
    query_text = args["query"]
    include_agents = args.get("include_agents", "")

    banks = [coord_bank]
    if include_agents:
        for role in include_agents.split(","):
            role = role.strip()
            if role:
                banks.append(agent_bank_id(session_id, role))

    result = await brain.reflect(query_text, banks=banks, strategy="parallel")
    return _format_sdk_response(result.answer)


async def _handle_subagent_recall(
    brain: Astrocyte,
    own_bank: str,
    coord_bank: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Sub-agent recall: search own bank + coordinator bank."""
    query_text = args["query"]
    max_results = args.get("max_results", 10)

    result = await brain.recall(
        query_text,
        banks=[own_bank, coord_bank],
        strategy="parallel",
        max_results=max_results,
    )
    hits = [
        {"text": h.text, "score": round(h.score, 4), "bank_id": h.bank_id}
        for h in result.hits
    ]
    return _format_sdk_response({"hits": hits, "total": result.total_available})


# ---------------------------------------------------------------------------
# Single-agent: session-scoped memory server
# ---------------------------------------------------------------------------

def create_memory_server(
    brain: Astrocyte,
    *,
    session_id: str,
    server_name: str = "astrocyte_memory",
    include_reflect: bool = True,
    include_forget: bool = False,
) -> Any:
    """Create an in-process MCP server with session-scoped memory.

    The bank_id is derived from the SDK session_id, so each session
    gets its own isolated memory namespace.

    Requires ``claude_agent_sdk`` to be installed.
    Returns an MCP server for ``ClaudeAgentOptions.mcp_servers``.
    """
    from astrocyte.integrations.claude_agent_sdk import astrocyte_claude_agent_server

    bank = session_bank_id(session_id)
    return astrocyte_claude_agent_server(
        brain,
        bank_id=bank,
        server_name=server_name,
        include_reflect=include_reflect,
        include_forget=include_forget,
    )


# ---------------------------------------------------------------------------
# Multi-agent: coordinator + sub-agent memory
# ---------------------------------------------------------------------------

def create_coordinator_server(
    brain: Astrocyte,
    *,
    session_id: str,
    server_name: str = "astrocyte_memory",
    include_reflect: bool = True,
) -> Any:
    """Create an MCP server for the coordinator agent.

    The coordinator writes to and reads from the shared coordinator bank.
    It can also read from any sub-agent bank via multi-bank recall.

    Requires ``claude_agent_sdk`` to be installed.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    coord_bank = coordinator_bank_id(session_id)

    sdk_tools = []

    @tool(
        "memory_retain",
        "Store a finding, decision, or coordination note into shared memory. "
        "All sub-agents can read this.",
        {"content": str, "tags": str},
    )
    async def memory_retain(args: dict[str, Any]) -> dict[str, Any]:
        return await _handle_retain(brain, coord_bank, args)

    sdk_tools.append(memory_retain)

    @tool(
        "memory_recall",
        "Search shared memory and optionally sub-agent memory banks. "
        "Pass agent roles as comma-separated 'include_agents' to also search their banks.",
        {"query": str, "max_results": int, "include_agents": str},
    )
    async def memory_recall(args: dict[str, Any]) -> dict[str, Any]:
        return await _handle_coordinator_recall(brain, session_id, coord_bank, args)

    sdk_tools.append(memory_recall)

    if include_reflect:

        @tool(
            "memory_reflect",
            "Synthesize a comprehensive answer from shared memory and sub-agent banks.",
            {"query": str, "include_agents": str},
        )
        async def memory_reflect(args: dict[str, Any]) -> dict[str, Any]:
            return await _handle_coordinator_reflect(brain, session_id, coord_bank, args)

        sdk_tools.append(memory_reflect)

    return create_sdk_mcp_server(
        name=server_name,
        version="1.0.0",
        tools=sdk_tools,
    )


def create_subagent_memory_server(
    brain: Astrocyte,
    *,
    session_id: str,
    role: str,
    server_name: str | None = None,
) -> Any:
    """Create an MCP server for a sub-agent's private memory bank.

    The sub-agent writes to its own bank (``session-{session_id}:agent-{role}``)
    and can read from both its own bank and the shared coordinator bank.

    Requires ``claude_agent_sdk`` to be installed.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    own_bank = agent_bank_id(session_id, role)
    coord_bank = coordinator_bank_id(session_id)
    name = server_name or f"astrocyte_memory_{role}"

    sdk_tools = []

    @tool(
        "memory_retain",
        f"Store a finding into the {role} agent's private memory bank.",
        {"content": str, "tags": str},
    )
    async def memory_retain(args: dict[str, Any]) -> dict[str, Any]:
        return await _handle_retain(brain, own_bank, args)

    sdk_tools.append(memory_retain)

    @tool(
        "memory_recall",
        f"Search the {role} agent's memory and the shared coordinator memory.",
        {"query": str, "max_results": int},
    )
    async def memory_recall(args: dict[str, Any]) -> dict[str, Any]:
        return await _handle_subagent_recall(brain, own_bank, coord_bank, args)

    sdk_tools.append(memory_recall)

    return create_sdk_mcp_server(
        name=name,
        version="1.0.0",
        tools=sdk_tools,
    )


def create_subagent_definition(
    brain: Astrocyte,
    *,
    role: str,
    session_id: str,
    description: str,
    prompt: str,
    tools: list[str] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Create an AgentDefinition dict for a sub-agent with its own memory bank.

    Returns a dict compatible with ``ClaudeAgentOptions.agents``.
    The sub-agent gets an MCP server that writes to its own bank
    and reads from both its bank and the coordinator bank.
    """
    memory_server = create_subagent_memory_server(
        brain, session_id=session_id, role=role
    )
    server_name = f"astrocyte_memory_{role}"

    definition: dict[str, Any] = {
        "description": description,
        "prompt": prompt,
        "mcpServers": [memory_server],
    }

    agent_tools = list(tools or [])
    agent_tools.append(f"mcp__{server_name}__*")
    definition["tools"] = agent_tools

    if model:
        definition["model"] = model

    return definition
