"""End-to-end test: Claude Managed Agents + Astrocyte memory.

Runs a real Managed Agents session with Astrocyte memory wired as custom tools.
Requires ANTHROPIC_API_KEY and the anthropic SDK installed.

This test is skipped in normal CI. It runs in a dedicated job
triggered by push to main or manual dispatch.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

_skip_reason_key = "ANTHROPIC_API_KEY not set"
_skip_reason_sdk = "anthropic SDK not installed"

_has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

try:
    import anthropic  # noqa: F401
    _has_sdk = True
except ImportError:
    _has_sdk = False

_skip = pytest.mark.skipif(
    not _has_sdk or not _has_key,
    reason=_skip_reason_sdk if not _has_sdk else _skip_reason_key,
)

from astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.integrations.claude_managed_agents import (
    handle_memory_tool,
    is_memory_tool,
    memory_tool_definitions,
)
from astrocyte.testing.in_memory import InMemoryEngineProvider


def _make_brain() -> Astrocyte:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain


def _make_client():
    """Create an Anthropic client with the managed-agents beta header."""
    from anthropic import Anthropic

    return Anthropic()


@_skip
async def test_retain_and_recall_via_managed_agent() -> None:
    """Agent stores a fact via memory_retain, then recalls it via memory_recall."""
    client = _make_client()
    brain = _make_brain()
    bank_id = "e2e-managed-test"

    # Create agent with memory tools + built-in toolset
    agent = client.beta.agents.create(
        name="Astrocyte Memory Test Agent",
        model="claude-sonnet-4-6",
        system=(
            "You are a test agent with long-term memory. "
            "When asked to store something, use memory_retain. "
            "When asked to recall something, use memory_recall. "
            "Always use the memory tools when instructed to do so."
        ),
        tools=memory_tool_definitions(include_reflect=False, include_forget=False),
    )

    # Create environment
    environment = client.beta.environments.create(
        name="astrocyte-e2e-test-env",
        config={
            "type": "cloud",
            "networking": {"type": "unrestricted"},
        },
    )

    try:
        # --- Turn 1: Store a fact ---
        session = client.beta.sessions.create(
            agent=agent.id,
            environment_id=environment.id,
            title="Astrocyte e2e: retain + recall",
        )

        agent_text = await _run_turn(
            client, brain, session.id, bank_id,
            prompt=(
                "Use the memory_retain tool to store this exact fact: "
                "'Astrocyte is an open-source memory framework for AI agents.' "
                "Then confirm you stored it."
            ),
        )

        assert agent_text, "Agent should have produced a response for retain"
        print(f"[Turn 1 — Retain] {agent_text[:200]}")

        # --- Turn 2: Recall the fact (same session) ---
        agent_text = await _run_turn(
            client, brain, session.id, bank_id,
            prompt=(
                "Use the memory_recall tool to search for 'Astrocyte'. "
                "Tell me what you found."
            ),
        )

        assert agent_text, "Agent should have produced a response for recall"
        assert "astrocyte" in agent_text.lower() or "memory" in agent_text.lower(), (
            f"Recall result should mention Astrocyte or memory, got: {agent_text[:200]}"
        )
        print(f"[Turn 2 — Recall] {agent_text[:200]}")

        # Clean up session
        client.beta.sessions.delete(session.id)

    finally:
        # Clean up resources
        try:
            client.beta.environments.delete(environment.id)
        except Exception:
            pass
        try:
            client.beta.agents.delete(agent.id)
        except Exception:
            pass


async def _run_turn(
    client,
    brain: Astrocyte,
    session_id: str,
    bank_id: str,
    prompt: str,
) -> str:
    """Send a prompt and handle events until the agent finishes."""
    import json

    agent_text_parts: list[str] = []
    events_by_id: dict[str, object] = {}

    with client.beta.sessions.events.stream(session_id) as stream:
        # Send the user message after the stream opens
        client.beta.sessions.events.send(
            session_id,
            events=[
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": prompt}],
                },
            ],
        )

        for event in stream:
            event_type = event.type
            print(f"  [event] {event_type}")

            if event_type == "agent.message":
                for block in event.content:
                    if hasattr(block, "text"):
                        agent_text_parts.append(block.text)

            elif event_type == "agent.custom_tool_use":
                events_by_id[event.id] = event
                print(f"  [custom_tool] {event.name}({json.dumps(event.input)[:100]})")

            elif event_type == "session.status_idle":
                stop = getattr(event, "stop_reason", None)
                if stop is None:
                    continue

                if stop.type == "requires_action":
                    for event_id in stop.event_ids:
                        tool_event = events_by_id.get(event_id)
                        if tool_event is None:
                            continue

                        if is_memory_tool(tool_event.name):
                            result_text = await handle_memory_tool(
                                brain,
                                tool_event.name,
                                tool_event.input,
                                bank_id=bank_id,
                            )
                            print(f"  [tool_result] {result_text[:100]}")
                        else:
                            result_text = json.dumps({
                                "error": f"Unknown tool: {tool_event.name}"
                            })

                        client.beta.sessions.events.send(
                            session_id,
                            events=[
                                {
                                    "type": "user.custom_tool_result",
                                    "custom_tool_use_id": event_id,
                                    "content": [
                                        {"type": "text", "text": result_text}
                                    ],
                                },
                            ],
                        )

                elif stop.type == "end_turn":
                    break

            elif event_type == "session.status_terminated":
                error = getattr(event, "error", None)
                raise RuntimeError(f"Session terminated: {error}")

    return "".join(agent_text_parts)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    print("=== E2E: Claude Managed Agents + Astrocyte ===\n")

    print("--- Test: Retain and recall via Managed Agent ---")
    await test_retain_and_recall_via_managed_agent()
    print()

    print("=== All e2e tests passed ===")


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set, skipping e2e tests")
        sys.exit(0)

    asyncio.run(main())
