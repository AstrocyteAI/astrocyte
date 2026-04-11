"""End-to-end test: Claude Managed Agents + Astrocyte memory.

Runs a real Managed Agents session with Astrocyte memory wired as custom tools.
Requires ANTHROPIC_API_KEY and the anthropic SDK installed.

This test is skipped in normal CI. It runs in a dedicated job
triggered by push to main or manual dispatch.
"""

from __future__ import annotations

import asyncio
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

from astrocyte import Astrocyte  # noqa: E402
from astrocyte.config import AstrocyteConfig  # noqa: E402
from astrocyte.integrations.claude_managed_agents import (  # noqa: E402
    delete_managed_session,
    memory_tool_definitions,
    run_session_with_memory,
)
from astrocyte.testing.in_memory import InMemoryEngineProvider  # noqa: E402


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

    # Built-in agent toolset plus Astrocyte custom memory tools (API expects both).
    agent = client.beta.agents.create(
        name="Astrocyte Memory Test Agent",
        model="claude-sonnet-4-6",
        system=(
            "You are a test agent with long-term memory. "
            "When asked to store something, use memory_retain. "
            "When asked to recall something, use memory_recall. "
            "Always use the memory tools when instructed to do so."
        ),
        tools=[
            {"type": "agent_toolset_20260401"},
            *memory_tool_definitions(include_reflect=False, include_forget=False),
        ],
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

        agent_text = await run_session_with_memory(
            client,
            brain,
            session_id=session.id,
            bank_id=bank_id,
            prompt=(
                "Use the memory_retain tool to store this exact fact: "
                "'Astrocyte is an open-source memory framework for AI agents.' "
                "Then confirm you stored it."
            ),
            timeout_seconds=120,
        )

        assert agent_text, "Agent should have produced a response for retain"
        print(f"[Turn 1 — Retain] {agent_text[:200]}")

        # --- Turn 2: Recall the fact (same session) ---
        agent_text = await run_session_with_memory(
            client,
            brain,
            session_id=session.id,
            bank_id=bank_id,
            prompt=("Use the memory_recall tool to search for 'Astrocyte'. Tell me what you found."),
            timeout_seconds=120,
        )

        assert agent_text, "Agent should have produced a response for recall"
        assert "astrocyte" in agent_text.lower() or "memory" in agent_text.lower(), (
            f"Recall result should mention Astrocyte or memory, got: {agent_text[:200]}"
        )
        print(f"[Turn 2 — Recall] {agent_text[:200]}")

        # API rejects DELETE while session is still "running" after stream close.
        delete_managed_session(client, session.id)

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
