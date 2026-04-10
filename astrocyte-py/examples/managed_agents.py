"""Example: Astrocyte + Claude Managed Agents.

Demonstrates three integration patterns:

1. Single agent with session-scoped memory
2. Multi-agent coordination with shared + private memory banks
3. Astrocyte MCP server as a remote SSE server

Prerequisites:
    pip install astrocyte claude-agent-sdk
    export ANTHROPIC_API_KEY=your-key
"""

from __future__ import annotations

import asyncio
import os

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    query,
)

from astrocyte import Astrocyte
from astrocyte.integrations.managed_agents import (
    create_coordinator_server,
    create_memory_server,
    create_subagent_definition,
)


# ---------------------------------------------------------------------------
# Pattern 1: Single agent with session-scoped memory
# ---------------------------------------------------------------------------

async def single_agent_example():
    """An agent that remembers across turns within a session."""

    brain = Astrocyte.from_config("astrocyte.yaml")
    session_id = None

    # First turn: the agent learns something
    memory_server = create_memory_server(brain, session_id="demo-session-001")

    options = ClaudeAgentOptions(
        mcp_servers={"memory": memory_server},
        allowed_tools=["mcp__memory__*"],
    )

    async for message in query(
        prompt=(
            "Remember: Calvin prefers dark mode, uses Python, "
            "and is building Astrocyte, an open-source memory framework."
        ),
        options=options,
    ):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            session_id = message.data.get("session_id")
        if isinstance(message, ResultMessage) and message.subtype == "success":
            print("[Turn 1]", message.result)

    # Second turn: the agent recalls what it learned
    async for message in query(
        prompt="What do you know about Calvin's preferences?",
        options=ClaudeAgentOptions(
            mcp_servers={"memory": memory_server},
            allowed_tools=["mcp__memory__*"],
            resume=session_id,
        ),
    ):
        if isinstance(message, ResultMessage) and message.subtype == "success":
            print("[Turn 2]", message.result)


# ---------------------------------------------------------------------------
# Pattern 2: Multi-agent with coordinated memory
# ---------------------------------------------------------------------------

async def multi_agent_example():
    """A coordinator dispatches to researcher and analyst sub-agents.

    Memory topology:
        session-demo:coordinator    <- shared, all agents read
        session-demo:agent-researcher  <- researcher writes here
        session-demo:agent-analyst     <- analyst writes here
    """

    brain = Astrocyte.from_config("astrocyte.yaml")
    sid = "demo-multi-001"

    # Coordinator gets the shared memory bank
    coord_server = create_coordinator_server(brain, session_id=sid)

    # Sub-agents get their own banks + read access to coordinator bank
    researcher = create_subagent_definition(
        brain,
        role="researcher",
        session_id=sid,
        description=(
            "Research specialist. Use this agent to explore codebases, "
            "search for patterns, and gather information."
        ),
        prompt=(
            "You are a research agent. Explore the codebase thoroughly. "
            "Store key findings in memory using memory_retain. "
            "Always cite file paths and line numbers."
        ),
        tools=["Read", "Grep", "Glob"],
        model="sonnet",
    )

    analyst = create_subagent_definition(
        brain,
        role="analyst",
        session_id=sid,
        description=(
            "Analysis specialist. Use this agent to synthesize findings, "
            "identify patterns, and produce recommendations."
        ),
        prompt=(
            "You are an analysis agent. Recall findings from the researcher "
            "and coordinator memory banks. Synthesize insights and store "
            "your analysis in memory."
        ),
        tools=["Read"],
        model="sonnet",
    )

    options = ClaudeAgentOptions(
        mcp_servers={"memory": coord_server},
        allowed_tools=["mcp__memory__*", "Agent"],
        agents={
            "researcher": AgentDefinition(**researcher),
            "analyst": AgentDefinition(**analyst),
        },
    )

    async for message in query(
        prompt=(
            "Investigate the Astrocyte codebase architecture. "
            "Have the researcher explore the code structure, then "
            "have the analyst synthesize the findings. "
            "Store a summary in shared memory."
        ),
        options=options,
    ):
        if isinstance(message, ResultMessage) and message.subtype == "success":
            print("[Coordinator]", message.result)


# ---------------------------------------------------------------------------
# Pattern 3: Remote MCP server (for any Managed Agent)
# ---------------------------------------------------------------------------

def remote_mcp_config() -> dict:
    """Config snippet for connecting to Astrocyte's MCP server remotely.

    Run the server:
        astrocyte-mcp --config astrocyte.yaml --transport sse --port 8080

    Then any Managed Agent can connect via SSE:
    """
    return {
        "astrocyte": {
            "type": "sse",
            "url": os.environ.get("ASTROCYTE_MCP_URL", "http://localhost:8080/mcp/sse"),
        }
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "multi":
        asyncio.run(multi_agent_example())
    else:
        asyncio.run(single_agent_example())
