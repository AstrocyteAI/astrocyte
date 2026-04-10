"""End-to-end test: Claude Agent SDK + Astrocyte memory.

Runs a real Claude Agent SDK query with Astrocyte wired as an MCP server.
Requires ANTHROPIC_API_KEY and claude-agent-sdk installed.

This test is skipped in normal CI. It runs only when the
``e2e-claude-agent-sdk`` workflow job is manually dispatched in GitHub Actions.

Billing note: this path uses the **Claude Code CLI** (via the SDK). With
``ANTHROPIC_API_KEY`` set, usage is charged against your **Anthropic Console /
API account** (prepaid balance, workspace limits, etc.). That is not the same
meter as a **claude.ai** subscription UI or as **Managed Agents** REST calls
alone—those can succeed while the API wallet the CLI uses is empty.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys

import pytest

_skip_reason_sdk = "claude-agent-sdk not installed"
_skip_reason_key = "ANTHROPIC_API_KEY not set"

try:
    import claude_agent_sdk  # noqa: F401
    _has_sdk = True
except ImportError:
    _has_sdk = False

_has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

_skip = pytest.mark.skipif(
    not _has_sdk or not _has_key,
    reason=_skip_reason_sdk if not _has_sdk else _skip_reason_key,
)

from astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.testing.in_memory import InMemoryEngineProvider


def _make_brain() -> Astrocyte:
    config = AstrocyteConfig()
    config.provider = "test"
    config.barriers.pii.mode = "disabled"
    brain = Astrocyte(config)
    engine = InMemoryEngineProvider()
    brain.set_engine_provider(engine)
    return brain


def _resolve_cli_path() -> str | None:
    """CLI binary for Claude Agent SDK.

    The PyPI wheel bundles ``claude``, but that binary often exits immediately on
    headless Linux (e.g. GitHub Actions). Passing an explicit ``cli_path`` skips
    the bundled binary. Upstream CI installs the real CLI via install.sh.

    Override: ``CLAUDE_CODE_CLI_PATH``. On ``CI=true``, use ``shutil.which("claude")``
    when present so workflow-installed CLIs are used.
    """
    explicit = (os.environ.get("CLAUDE_CODE_CLI_PATH") or "").strip()
    if explicit:
        return explicit
    if os.environ.get("CI", "").lower() in ("1", "true", "yes"):
        found = shutil.which("claude")
        if found:
            return found
    return None


def _make_options(server):
    """Create ClaudeAgentOptions for CI.

    SDK MCP servers require ``ClaudeSDKClient``, not ``query()`` with a string
    prompt: ``query()`` closes stdin after the first result (see upstream
    ``e2e-tests/test_sdk_mcp_tools.py``), which breaks the control channel.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    # Tool IDs are mcp__<mcp_servers dict key>__<tool_name>, not the server's
    # internal create_sdk_mcp_server(name=...) — see claude_agent_sdk docs.
    cli_path = _resolve_cli_path()
    if cli_path:
        print(f"  [e2e] cli_path={cli_path}")
    return ClaudeAgentOptions(
        mcp_servers={"memory": server},
        allowed_tools=[
            "mcp__memory__memory_retain",
            "mcp__memory__memory_recall",
            "mcp__memory__memory_reflect",
        ],
        max_turns=6,
        permission_mode="bypassPermissions",
        stderr=lambda line: print(f"  [cli stderr] {line}"),
        **({"cli_path": cli_path} if cli_path else {}),
    )


def _fail_if_cli_billing_error(result_text: str, step: str) -> None:
    """Claude Code often reports API-wallet issues as a normal result string."""
    t = result_text.lower()
    if "credit balance is too low" in t or "insufficient credits" in t:
        pytest.fail(
            f"Claude Code CLI billing/quota error during {step}: {result_text!r}\n\n"
            "The Agent SDK shells out to Claude Code. With ANTHROPIC_API_KEY set, "
            "that flow uses your Anthropic API / Console spend, not claude.ai "
            "subscription credits shown in the consumer app. Managed Agents e2e "
            "hits the API directly and can pass with the same key while this job "
            "fails if API prepaid balance or org limits block Code. "
            "Top up or switch key in Console (Usage & billing) for the workspace "
            "that owns this API key."
        )


async def _run_turn(client, prompt: str) -> str:
    """Send one user turn and return the final result text (success subtype)."""
    from claude_agent_sdk import ResultMessage

    await client.query(prompt)
    result_text = ""
    async for message in client.receive_response():
        msg_type = getattr(message, "type", "?")
        msg_sub = getattr(message, "subtype", "?")
        print(f"  [msg] type={msg_type} subtype={msg_sub}")
        if isinstance(message, ResultMessage):
            result_text = message.result or ""
            if message.subtype != "success":
                print(f"  [error] {message.subtype}: {result_text[:500]}")
    _fail_if_cli_billing_error(result_text, "agent turn")
    return result_text


@_skip
async def test_single_agent_memory() -> None:
    """Agent stores a fact via memory_retain, then recalls it."""
    from claude_agent_sdk import ClaudeSDKClient

    from astrocyte.integrations.claude_agent_sdk import astrocyte_claude_agent_server

    brain = _make_brain()
    server = astrocyte_claude_agent_server(brain, bank_id="e2e-test")
    options = _make_options(server)

    async with ClaudeSDKClient(options=options) as client:
        result_text = await _run_turn(
            client,
            (
                "Use the memory_retain tool to store this exact fact: "
                "'Astrocyte is an open-source memory framework for AI agents.' "
                "Then confirm you stored it."
            ),
        )
        assert result_text, "Agent should have produced a result"
        print(f"[Turn 1 — Retain] {result_text[:200]}")

        result_text = await _run_turn(
            client,
            (
                "Use the memory_recall tool to search for 'Astrocyte'. "
                "Tell me what you found."
            ),
        )
        assert result_text, "Agent should have produced a result"
        assert "astrocyte" in result_text.lower() or "memory" in result_text.lower(), (
            f"Recall result should mention Astrocyte or memory, got: {result_text[:200]}"
        )
        print(f"[Turn 2 — Recall] {result_text[:200]}")


@_skip
async def test_managed_agents_session_memory() -> None:
    """Session-scoped memory server creates isolated banks."""
    from claude_agent_sdk import ClaudeSDKClient

    from astrocyte.integrations.managed_agents import create_memory_server

    brain = _make_brain()
    server = create_memory_server(brain, session_id="e2e-session-001")
    options = _make_options(server)

    async with ClaudeSDKClient(options=options) as client:
        result_text = await _run_turn(
            client,
            (
                "Use memory_retain to store: 'Session memory works end-to-end.' "
                "Then use memory_recall to search for 'session'. "
                "Report what you found."
            ),
        )

    assert result_text, "Agent should have produced a result"
    print(f"[Session memory] {result_text[:200]}")


async def main() -> None:
    print("=== E2E: Claude Agent SDK + Astrocyte ===\n")

    print("--- Test 1: Single agent memory ---")
    await test_single_agent_memory()
    print()

    print("--- Test 2: Managed Agents session memory ---")
    await test_managed_agents_session_memory()
    print()

    print("=== All e2e tests passed ===")


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set, skipping e2e tests")
        sys.exit(0)

    try:
        import claude_agent_sdk
    except ImportError:
        print("claude-agent-sdk not installed, skipping e2e tests")
        sys.exit(0)

    asyncio.run(main())
